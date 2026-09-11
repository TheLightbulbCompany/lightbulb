# Slack-parity messaging — Phase 0 + 1 (message list feel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the room timeline read like a conversation — newest message on open, auto-scroll with a "new messages" pill, author grouping, date dividers, a "New" line, mention chips, any-emoji reactions with a picker and quick reactions in the shipped hover toolbar, copy-link/permalinks, and an honest "no reply" line for silent agent turns.

**Architecture:** All frontend, all inside the existing canonical room contract. `RoomPanel.tsx` (547 lines) hands its timeline to two new focused components: pure `timeline.ts` helpers decide grouping/dividers/New-line from `messages` + the read cursor captured at room open; `RoomTimeline.tsx` owns the scroll container and the pill; `RoomMessageItem.tsx` is the extracted per-message article. Mentions become chips by rewriting `@tokens` into `mention:` links before `MarkdownView` and rendering them in the existing `a` override. Permalinks ride the existing one-way URL command channel (`?panel=chat&room=…&message=…`).

**Tech Stack:** Next.js 16 / React 19, `radix-ui` (Popover, DropdownMenu, Tooltip — already installed), `react-markdown` via `MarkdownView`, Vitest + Testing Library (`cd apps/frontend && pnpm test -- <path>`), ESLint. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-11-slack-parity-messaging-design.md` (Phase 0 and Phase 1 sections, "Message list" anatomy, "States and accessibility").

## Global Constraints

- No colour literals outside `globals.css` — tokens only (`var(--foreground)`, `var(--muted)`, `var(--border)`, `var(--info)`, …). `node scripts/brand-tokens-scan.mjs src` must print `0` (run from `apps/frontend`).
- Borders only, no shadows except the shipped `.room-message-actions` one; 4px spacing rhythm; radii from the existing `messaging.css` (4–8px); Source Sans via `var(--font-sans)`.
- "Never colour alone": every status keeps its word or icon. State colours mark text/icon/border/rule, never fills.
- Every new control has hover, focus-visible (`outline: 2px solid var(--ring)` is already applied to `.room-panel :is(button, …):focus-visible`), disabled, pending, and error states. Touch targets ≥44px at ≤859px (existing `@media (max-width: 859px)` rule covers `.room-panel button`).
- `prefers-reduced-motion: reduce` disables the pill slide (existing rule already zeroes transitions/animations under `.room-panel *`).
- No new dependencies. No feature flags, no dual paths, no compatibility shims.
- Never log message bodies. Never send message text anywhere new.
- Ownership: Claude's lane. Do not touch onboarding, billing, `WorkspaceShell` onboarding code, or backend messaging in this plan.
- Existing tests in `apps/frontend/src/components/messaging/__tests__/` and `apps/frontend/src/hooks/__tests__/useRooms.test.tsx` must keep passing after every task.
- Commits: conventional prefix (`feat(messaging): …`, `refactor(messaging): …`, `test(messaging): …`), ending with the attribution block from the session's system reminder.

---

## File structure

| File | Responsibility |
|---|---|
| `apps/frontend/src/components/messaging/timeline.ts` (create) | Pure helpers: `buildTimeline` (grouping, date dividers, New line), `dateDividerLabel`. No React. |
| `apps/frontend/src/components/messaging/RoomTimeline.tsx` (create) | The scroll container: renders timeline items, sticks to bottom, "new messages" pill, initial scroll-to-bottom, scroll/highlight to a target message. |
| `apps/frontend/src/components/messaging/RoomMessageItem.tsx` (create) | One message article (avatar, byline, content, reactions, attachments, run status, reply count, hover toolbar actions). Extracted from `RoomPanel.renderMessage`. |
| `apps/frontend/src/components/messaging/mentions.ts` (create) | `linkifyMentions(text, mentions, participants)` — rewrites `@tokens` to `mention:` links; `MENTION_SCHEME`. |
| `apps/frontend/src/components/messaging/emojiData.ts` (create) | Curated emoji list `{ emoji, name, keywords }` (~400) grouped by category. |
| `apps/frontend/src/components/messaging/EmojiPicker.tsx` (create) | Popover picker: search, recent row (localStorage), category grid, keyboard navigation. |
| `apps/frontend/src/components/messaging/messageLinks.ts` (create) | `messagePermalink(roomId, messageId)`, `copyText(text)`. |
| `apps/frontend/src/components/messaging/RoomPanel.tsx` (modify) | Uses `RoomTimeline` + `RoomMessageItem`; wires picker, quick reactions, copy link, target message; drops `reactionEmojis`. |
| `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (modify) | Carries `targetMessageId` (from the permalink) into `roomProps`; exposes `openMessageLink`. |
| `apps/frontend/src/components/workspace/panelRegistry.ts` (modify) | Chat descriptor accepts `room` (+ one-shot `message`). |
| `apps/frontend/src/components/workspace/panels/ChatPanel.tsx` (modify) | Applies `roomId`/`messageId` params: selects the room and sets the target message. |
| `apps/frontend/src/components/messaging/messaging.css` (modify) | Divider, New line, pill, collapsed-message gutter, mention chip, reaction chip, picker styles. |
| `apps/infra/openclaw/skills/lightbulb/references/surfaces.md` (modify) | Messages row: hover toolbar, reactions, copy link. |
| Tests | `__tests__/timeline.test.ts`, `__tests__/RoomTimeline.test.tsx`, `__tests__/mentions.test.ts`, `__tests__/EmojiPicker.test.tsx`, `__tests__/messageLinks.test.ts`, additions to `__tests__/RoomPanel.test.tsx`, `workspace/__tests__/urlChannel.test.ts` (exists — add cases). |

Types reused everywhere (from `apps/frontend/src/components/messaging/types.ts`): `RoomMessage` (`messageId`, `roomId`, `messageSeq`, `author: {kind, id, displayName}`, `text`, `mentions: MemberRef[]`, `threadRootMessageId`, `createdAt`, `runId`, `reactions?: Record<string, string[]>`, `deletedAt?`, `editedAt?`, `attachments?`), `RoomParticipant` (`kind`, `id`, `displayName`, `mentionHandle`, `listenMode`, `role`), `RoomRunView` (`runId`, `agentId`, `state`, `text`, `tools`).

---

# Phase 0 — Foundation

### Task 1: Timeline helpers (grouping, date dividers, New line)

**Files:**
- Create: `apps/frontend/src/components/messaging/timeline.ts`
- Test: `apps/frontend/src/components/messaging/__tests__/timeline.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export type TimelineItem =
    | { kind: "date"; key: string; label: string; date: Date }
    | { kind: "new"; key: string }
    | { kind: "message"; key: string; message: RoomMessage; collapsed: boolean };
  export function buildTimeline(messages: readonly RoomMessage[], options: { readSeq: number | null; now?: Date }): TimelineItem[];
  export function dateDividerLabel(date: Date, now: Date): string;
  export const GROUP_WINDOW_MS = 5 * 60 * 1000;
  ```
  Rules: a message is `collapsed` when the previous *message* item has the same `author.kind` + `author.id`, is ≤5 min older, no date divider or New line was emitted between them, and neither message is deleted. A `date` item precedes the first message of each local calendar day. A single `new` item precedes the first message with `messageSeq > readSeq` when `readSeq !== null` and such a message exists and it is not authored by `currentUserId`… — **no**: keep `buildTimeline` author-agnostic; the caller passes `readSeq: null` to suppress the line (Task 2 decides when).

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/components/messaging/__tests__/timeline.test.ts
import { describe, expect, it } from "vitest";
import { buildTimeline, dateDividerLabel } from "../timeline";
import type { RoomMessage } from "../types";

const base: RoomMessage = { messageId: "m", roomId: "r", messageSeq: 0, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-11T10:00:00Z", runId: null };
const at = (seq: number, iso: string, author = base.author, extra: Partial<RoomMessage> = {}): RoomMessage => ({ ...base, messageId: `m${seq}`, messageSeq: seq, createdAt: iso, author, ...extra });
const owner = { kind: "human" as const, id: "owner", displayName: "Owner" };
const now = new Date("2026-09-11T12:00:00Z");

describe("buildTimeline", () => {
  it("collapses consecutive same-author messages within five minutes", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:04:59Z"), at(3, "2026-09-11T10:05:00Z")], { readSeq: null, now });
    const messages = items.filter((item) => item.kind === "message");
    expect(messages.map((item) => item.kind === "message" && item.collapsed)).toEqual([false, true, true]);
  });
  it("does not collapse across authors, deletions, or a five-minute gap", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z", owner), at(3, "2026-09-11T10:02:00Z", owner, { deletedAt: "2026-09-11T10:03:00Z" }), at(4, "2026-09-11T10:03:00Z", owner), at(5, "2026-09-11T10:09:00Z", owner)], { readSeq: null, now });
    expect(items.filter((item) => item.kind === "message").map((item) => item.kind === "message" && item.collapsed)).toEqual([false, false, false, false, false]);
  });
  it("emits one date divider per local day and never collapses across it", () => {
    const items = buildTimeline([at(1, "2026-09-10T23:59:00Z"), at(2, "2026-09-11T00:01:00Z")], { readSeq: null, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "message", "date", "message"]);
    expect(items[3]).toMatchObject({ kind: "message", collapsed: false });
  });
  it("emits the New line before the first unread message and breaks grouping there", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z"), at(3, "2026-09-11T10:02:00Z")], { readSeq: 1, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "message", "new", "message", "message"]);
    expect(items[3]).toMatchObject({ kind: "message", collapsed: false });
    expect(items[4]).toMatchObject({ kind: "message", collapsed: true });
  });
  it("emits no New line when everything is read or readSeq is null", () => {
    const messages = [at(1, "2026-09-11T10:00:00Z")];
    expect(buildTimeline(messages, { readSeq: 1, now }).some((item) => item.kind === "new")).toBe(false);
    expect(buildTimeline(messages, { readSeq: null, now }).some((item) => item.kind === "new")).toBe(false);
  });
  it("gives every item a stable unique key", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z")], { readSeq: 1, now });
    expect(new Set(items.map((item) => item.key)).size).toBe(items.length);
  });
});

describe("dateDividerLabel", () => {
  it("labels today, yesterday, weekday within a week, otherwise a full date", () => {
    expect(dateDividerLabel(new Date("2026-09-11T09:00:00Z"), now)).toBe("Today");
    expect(dateDividerLabel(new Date("2026-09-10T09:00:00Z"), now)).toBe("Yesterday");
    expect(dateDividerLabel(new Date("2026-09-08T09:00:00Z"), now)).toBe(new Date("2026-09-08T09:00:00Z").toLocaleDateString(undefined, { weekday: "long" }));
    expect(dateDividerLabel(new Date("2026-08-01T09:00:00Z"), now)).toBe(new Date("2026-08-01T09:00:00Z").toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }));
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/timeline.test.ts`
Expected: FAIL — `Cannot find module '../timeline'`.

- [ ] **Step 3: Implement `timeline.ts`**

```ts
// apps/frontend/src/components/messaging/timeline.ts
import type { RoomMessage } from "./types";

export const GROUP_WINDOW_MS = 5 * 60 * 1000;

export type TimelineItem =
  | { kind: "date"; key: string; label: string; date: Date }
  | { kind: "new"; key: string }
  | { kind: "message"; key: string; message: RoomMessage; collapsed: boolean };

const dayKey = (date: Date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;

export function dateDividerLabel(date: Date, now: Date): string {
  const startOf = (value: Date) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(date)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days > 1 && days < 7) return date.toLocaleDateString(undefined, { weekday: "long" });
  return date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

export function buildTimeline(messages: readonly RoomMessage[], { readSeq, now = new Date() }: { readSeq: number | null; now?: Date }): TimelineItem[] {
  const items: TimelineItem[] = [];
  let lastDay: string | null = null;
  let previous: RoomMessage | null = null;
  let newLineDone = readSeq === null;
  for (const message of messages) {
    const created = new Date(message.createdAt);
    const valid = !Number.isNaN(created.getTime());
    let boundary = false;
    if (valid && dayKey(created) !== lastDay) {
      lastDay = dayKey(created);
      items.push({ kind: "date", key: `date:${lastDay}`, label: dateDividerLabel(created, now), date: created });
      boundary = true;
    }
    if (!newLineDone && message.messageSeq > (readSeq as number)) {
      items.push({ kind: "new", key: "new" });
      newLineDone = true;
      boundary = true;
    }
    const collapsed = !boundary && previous !== null && !message.deletedAt && !previous.deletedAt
      && previous.author.kind === message.author.kind && previous.author.id === message.author.id
      && valid && created.getTime() - new Date(previous.createdAt).getTime() <= GROUP_WINDOW_MS;
    items.push({ kind: "message", key: `message:${message.messageId}`, message, collapsed });
    previous = message;
  }
  return items;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/timeline.test.ts`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/messaging/timeline.ts apps/frontend/src/components/messaging/__tests__/timeline.test.ts
git commit -m "feat(messaging): timeline grouping, date dividers, and New line helpers"
```

---

### Task 2: `RoomTimeline` — newest on open, stick-to-bottom, "new messages" pill, New line

**Files:**
- Create: `apps/frontend/src/components/messaging/RoomTimeline.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:452-460` (the `role="region" aria-label="Messages"` block) and the `searchTarget` effect at `:281-288`
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomTimeline.test.tsx`

**Interfaces:**
- Consumes: `buildTimeline`, `TimelineItem` (Task 1).
- Produces:
  ```tsx
  export interface RoomTimelineProps {
    roomId: string;
    messages: readonly RoomMessage[];          // main (non-thread) messages, ascending seq
    readSeq: number | null;                    // read cursor captured at room open; null = no New line
    ownLastSentSeq: number | null;             // seq of the viewer's most recent message (forces scroll-to-bottom)
    targetMessageId?: string | null;           // scroll + highlight this message when it becomes present
    renderMessage: (message: RoomMessage, collapsed: boolean) => ReactNode;
    children?: ReactNode;                      // trailing content (pending messages, live runs)
    empty: ReactNode;                          // empty state
  }
  export function RoomTimeline(props: RoomTimelineProps): ReactElement;
  ```
  Behaviour: on mount / room change, scroll to bottom. When `messages` grows: if the container was within 48px of the bottom, or the newest message's seq equals `ownLastSentSeq`, scroll to bottom; otherwise increment `unseen` and show the pill "N new messages" (button → scroll to bottom, clears). Scrolling to the bottom clears the pill. `targetMessageId` scrolls that message into view (`block: "center"`), focuses it, and applies `data-highlight` for 2s (CSS handles the tint with `bg-accent` like search does today). Date items render `<div role="separator" aria-label={label} class="room-date-divider">`; the New item renders `<div role="separator" aria-label="New messages" class="room-new-line">`.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/frontend/src/components/messaging/__tests__/RoomTimeline.test.tsx
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { RoomTimeline } from "../RoomTimeline";
import type { RoomMessage } from "../types";

const base: RoomMessage = { messageId: "m", roomId: "r", messageSeq: 0, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-11T10:00:00Z", runId: null };
const msg = (seq: number, minute: number): RoomMessage => ({ ...base, messageId: `m${seq}`, messageSeq: seq, createdAt: `2026-09-11T10:${String(minute).padStart(2, "0")}:00Z` });

function scrollable(region: HTMLElement, { height = 400, content = 1000, top }: { height?: number; content?: number; top: number }) {
  Object.defineProperty(region, "clientHeight", { configurable: true, value: height });
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: content });
  let scrollTop = top;
  Object.defineProperty(region, "scrollTop", { configurable: true, get: () => scrollTop, set: (value: number) => { scrollTop = value; } });
}

const renderMessage = (message: RoomMessage, collapsed: boolean) => <p data-collapsed={collapsed}>{message.text}</p>;

it("renders separators for dates and the New line, and collapsed flags", () => {
  render(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={1} ownLastSentSeq={null} renderMessage={renderMessage} empty={<p>Empty</p>} />);
  expect(screen.getByRole("separator", { name: "Today" })).toBeInTheDocument();
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();
  expect(screen.getAllByText("x").map((node) => node.getAttribute("data-collapsed"))).toEqual(["false", "false"]);
});

it("shows the empty state when there are no messages", () => {
  render(<RoomTimeline roomId="r" messages={[]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={<p>Start the conversation</p>} />);
  expect(screen.getByText("Start the conversation")).toBeInTheDocument();
});

it("scrolls to the bottom on mount and when already at the bottom", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 600 });
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  expect(region.scrollTop).toBe(region.scrollHeight);
  expect(screen.queryByRole("button", { name: /new message/ })).not.toBeInTheDocument();
});

it("shows a pill instead of yanking the viewport when scrolled up, and clears it on click", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  fireEvent.scroll(region);
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1), msg(3, 2)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  expect(region.scrollTop).toBe(0);
  const pill = screen.getByRole("button", { name: "2 new messages" });
  fireEvent.click(pill);
  expect(region.scrollTop).toBe(region.scrollHeight);
  expect(screen.queryByRole("button", { name: /new message/ })).not.toBeInTheDocument();
});

it("always scrolls to the bottom for the viewer's own send", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  fireEvent.scroll(region);
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={2} renderMessage={renderMessage} empty={null} />);
  expect(region.scrollTop).toBe(region.scrollHeight);
});

it("scrolls to and highlights a target message", () => {
  vi.useFakeTimers();
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  render(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} targetMessageId="m1" renderMessage={renderMessage} empty={null} />);
  const article = screen.getAllByText("x")[0].closest("[data-message-id]")!;
  expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
  expect(article).toHaveAttribute("data-highlight", "true");
  act(() => { vi.advanceTimersByTime(2000); });
  expect(article).not.toHaveAttribute("data-highlight");
  vi.useRealTimers();
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomTimeline.test.tsx`
Expected: FAIL — `Cannot find module '../RoomTimeline'`.

- [ ] **Step 3: Implement `RoomTimeline.tsx`**

```tsx
// apps/frontend/src/components/messaging/RoomTimeline.tsx
"use client";

import { useEffect, useLayoutEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { buildTimeline } from "./timeline";
import type { RoomMessage } from "./types";
import "./messaging.css";

export interface RoomTimelineProps {
  roomId: string;
  messages: readonly RoomMessage[];
  readSeq: number | null;
  ownLastSentSeq: number | null;
  targetMessageId?: string | null;
  renderMessage: (message: RoomMessage, collapsed: boolean) => ReactNode;
  children?: ReactNode;
  empty: ReactNode;
}

const BOTTOM_THRESHOLD_PX = 48;
const HIGHLIGHT_MS = 2000;

export function RoomTimeline({ roomId, messages, readSeq, ownLastSentSeq, targetMessageId, renderMessage, children, empty }: RoomTimelineProps): ReactElement {
  const region = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const lastSeq = useRef<number>(messages.at(-1)?.messageSeq ?? 0);
  const [unseen, setUnseen] = useState(0);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const items = buildTimeline(messages, { readSeq });

  function scrollToBottom() {
    const node = region.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
    atBottom.current = true;
    setUnseen(0);
  }

  // Room change: newest message on open.
  useLayoutEffect(() => { scrollToBottom(); lastSeq.current = messages.at(-1)?.messageSeq ?? 0; /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [roomId]);

  // Growth: stick to the bottom, or count unseen and show the pill.
  useLayoutEffect(() => {
    const newest = messages.at(-1)?.messageSeq ?? 0;
    if (newest <= lastSeq.current) return;
    const added = messages.filter((message) => message.messageSeq > lastSeq.current).length;
    lastSeq.current = newest;
    if (atBottom.current || (ownLastSentSeq !== null && newest === ownLastSentSeq)) scrollToBottom();
    else setUnseen((current) => current + added);
  }, [messages, ownLastSentSeq]);

  useEffect(() => {
    if (!targetMessageId) return;
    const node = region.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(targetMessageId)}"]`);
    if (!node) return;
    node.scrollIntoView({ block: "center" });
    node.focus({ preventScroll: true });
    setHighlighted(targetMessageId);
    const timer = setTimeout(() => setHighlighted(null), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [targetMessageId, messages.length]);

  function onScroll() {
    const node = region.current;
    if (!node) return;
    atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_THRESHOLD_PX;
    if (atBottom.current) setUnseen(0);
  }

  return <div className="room-timeline">
    <div ref={region} role="region" aria-label="Messages" className="room-history" tabIndex={0} onScroll={onScroll}>
      {items.length === 0 ? empty : items.map((item) => {
        if (item.kind === "date") return <div key={item.key} role="separator" aria-label={item.label} className="room-date-divider"><span>{item.label}</span></div>;
        if (item.kind === "new") return <div key={item.key} role="separator" aria-label="New messages" className="room-new-line"><span>New</span></div>;
        return <div key={item.key} data-message-id={item.message.messageId} data-highlight={highlighted === item.message.messageId ? "true" : undefined} tabIndex={-1} className="room-timeline-item">
          {renderMessage(item.message, item.collapsed)}
        </div>;
      })}
      {children}
    </div>
    {unseen > 0 && <Button type="button" variant="outline" className="room-new-pill" onClick={scrollToBottom} aria-label={`${unseen} new ${unseen === 1 ? "message" : "messages"}`}>
      <ArrowDown size={14} aria-hidden="true" />{unseen} new {unseen === 1 ? "message" : "messages"}
    </Button>}
  </div>;
}
```

`CSS.escape` is not defined in jsdom: add to `apps/frontend/src/components/messaging/RoomTimeline.tsx` top: `const escapeSelector = (value: string) => typeof CSS !== "undefined" && "escape" in CSS ? CSS.escape(value) : value.replace(/["\\]/g, "\\$&");` and use it instead of `CSS.escape`.

- [ ] **Step 4: Add the styles**

Append to `apps/frontend/src/components/messaging/messaging.css` (after the `.room-history` rule at line 21):

```css
.room-timeline { position: relative; display: flex; flex-direction: column; min-height: 0; flex: 1; }
.room-timeline-item { outline: none; border-radius: 6px; }
.room-timeline-item[data-highlight="true"] { background: var(--accent); }
.room-date-divider { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 12px; margin: 12px 0 4px; font-size: .75rem; font-weight: 600; color: var(--muted-foreground); }
.room-date-divider::before, .room-date-divider::after { content: ""; flex: 1; border-top: 1px solid var(--border); }
.room-date-divider span { padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px; background: var(--background); }
.room-new-line { display: flex; align-items: center; gap: 8px; margin: 8px 0 4px; font-size: .75rem; font-weight: 600; color: var(--err); }
.room-new-line::after { content: ""; flex: 1; border-top: 1px solid var(--err); }
.room-new-pill { position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); z-index: 3; gap: 6px; border-radius: 999px; }
```

(The sticky divider needs `.room-history { position: relative }` — it already scrolls; sticky works against the scroll container.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomTimeline.test.tsx`
Expected: PASS (6 tests). If the "scrolls to the bottom on mount" test fails because `useLayoutEffect` ran before `scrollable()` defined the metrics, that is expected — the test rerenders after defining them; the growth effect performs the scroll.

- [ ] **Step 6: Wire `RoomTimeline` into `RoomPanel`**

In `RoomPanel.tsx`:
1. `import { RoomTimeline } from "./RoomTimeline";`
2. Add state near line 194: `const [openReadSeq] = useState(() => room?.lastReadSeq ?? null);` — **`RoomSummary` does not carry `lastReadSeq`; `RoomDetail` does.** Add to `RoomPanelProps` (after `room?: RoomSummary;`): `readSeqAtOpen?: number | null;` and in `MessagingWorkspaceContext.tsx` (Task 2 Step 7) pass it. Inside `RoomContent` compute `const newLineSeq = readSeqAtOpen ?? null;` and clear it once the viewer sends: keep `const [sentSinceOpen, setSentSinceOpen] = useState(false);` and pass `readSeq={sentSinceOpen ? null : newLineSeq}`; set `sentSinceOpen` in `retainAttempt`'s `invoke` — simpler: derive `ownLastSentSeq` and `sentSinceOpen` from `messages`: `const ownSeqs = messages.filter((m) => m.author.kind === "human" && m.author.id === currentUserId).map((m) => m.messageSeq); const ownLastSentSeq = ownSeqs.length ? Math.max(...ownSeqs) : null; const sentSinceOpen = ownLastSentSeq !== null && newLineSeq !== null && ownLastSentSeq > newLineSeq;`
3. Replace the block at lines 452-460 with:
```tsx
<RoomTimeline roomId={roomId} messages={mainMessages} readSeq={sentSinceOpen ? null : newLineSeq} ownLastSentSeq={ownLastSentSeq}
  targetMessageId={searchTarget && !searchTarget.threadRootMessageId ? searchTarget.messageId : targetMessageId ?? null}
  renderMessage={(message, collapsed) => renderMessage(message, false, collapsed)}
  empty={<div className="room-empty"><h3 className="font-semibold text-lg">Start the conversation</h3><p className="text-sm text-muted-foreground">Share an update or mention an agent when you want help.</p></div>}>
  {pendingMessages.filter((message) => message.threadRootMessageId === null).map((message) => <article key={message.clientMessageId} className="room-message" aria-label={`Pending message from ${viewer?.displayName ?? "You"}`}>
    <Identity name={viewer?.displayName ?? "You"} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{viewer?.displayName ?? "You"}</span><span className="text-sm text-muted-foreground">Sending…</span></div><MarkdownView skipHtml components={roomMarkdown}>{message.text}</MarkdownView></div>
  </article>)}
  {runs.filter((run) => !run.threadRootMessageId).map(renderRun)}
</RoomTimeline>
```
   Add `targetMessageId?: string | null;` to `RoomPanelProps` (used by Task 7; pass `undefined` for now). Change `renderMessage`'s signature to `(message: RoomMessage, inThread = false, collapsed = false)` — `collapsed` is unused until Task 4; keep the parameter so the call compiles.
4. Delete the main-timeline half of the `searchTarget` effect (lines 281-288): keep it only for `root` (thread) targets:
```tsx
useEffect(() => {
  if (!searchTarget?.threadRootMessageId) return;
  if (selection?.kind !== "thread" || selection.id !== searchTarget.threadRootMessageId) return;
  const target = document.getElementById(`${id}-thread-${searchTarget.messageId}`);
  target?.scrollIntoView({ block: "center" });
  target?.focus({ preventScroll: true });
}, [id, searchTarget, selection]);
```
   The main-timeline case is now handled by `RoomTimeline`'s `targetMessageId`.

- [ ] **Step 7: Pass `readSeqAtOpen` from the provider**

In `MessagingWorkspaceContext.tsx`, in the `roomProps` object (line ~301 onward) add `readSeqAtOpen: rooms.activeRoom?.lastReadSeq ?? null,` **captured once per room**: above `roomProps`, add
```tsx
const readSeqAtOpen = useRef<{ roomId: string; seq: number | null } | null>(null);
if (rooms.activeRoom && readSeqAtOpen.current?.roomId !== rooms.activeRoom.roomId) readSeqAtOpen.current = { roomId: rooms.activeRoom.roomId, seq: rooms.activeRoom.lastReadSeq ?? null };
```
and pass `readSeqAtOpen: readSeqAtOpen.current?.roomId === rooms.activeRoomId ? readSeqAtOpen.current.seq : null,`.

- [ ] **Step 8: Run the whole messaging suite and lint**

Run: `cd apps/frontend && pnpm test -- src/components/messaging src/hooks/__tests__/useRooms.test.tsx && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: all PASS; scan prints `0`. If `RoomPanel.test.tsx` cases that query `screen.getByRole("region", { name: "Messages" })` fail on scroll metrics, jsdom returns 0 for `scrollHeight` — the component tolerates that (0 ≤ 48 = at bottom).

- [ ] **Step 9: Commit**

```bash
git add apps/frontend/src/components/messaging/RoomTimeline.tsx apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/RoomTimeline.test.tsx
git commit -m "feat(messaging): room timeline with newest-on-open, sticky bottom, new-messages pill, and New line"
```

---

### Task 3 (controller, not an implementer): DESIGN.md, baseline critique, plan cleanup, ship Phase 0

**Files:**
- Create: `DESIGN.md` (repo root) — via the impeccable documenter subagent (`impeccable:impeccable-documenter`) with `PRODUCT.md`, `apps/frontend/.interface-design/system.md`, `apps/frontend/src/app/globals.css`, and `apps/frontend/src/components/messaging/messaging.css` as inputs; boundary: the messaging surface.
- Delete: `docs/superpowers/plans/2026-09-11-slack-like-essentials.md` (Codex's superseded plan; both its open items merged).

- [ ] **Step 1:** Spawn `impeccable:impeccable-documenter` (project root `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync`, artifact `apps/frontend/src/components/messaging/`, direction: "established world — document, do not invent"). Read the resulting `DESIGN.md`; reject any invented token or rule not present in `globals.css`/`system.md`.
- [ ] **Step 2:** Run `"/Users/prasiddhaparthsarthy/.claude/plugins/cache/impeccable/impeccable/4.2.2/skills/impeccable/scripts/impeccable" detect --json --target apps/frontend/src/components/messaging` from the repo root; record findings in the PR description; fix mechanical ones in this PR.
- [ ] **Step 3:** `git rm docs/superpowers/plans/2026-09-11-slack-like-essentials.md`.
- [ ] **Step 4:** Open the PR (`feat(messaging): phase 0 — timeline foundation`) from a worktree branch `feat/slack-parity-phase-0`; wait for `claude-review`; merge; watch the dev deploy (`gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`); verify on `https://dev.flickmeon.ai/chat` with the e2e account: opening the founder channel lands on the newest message; a message sent from a second tab while scrolled up shows the pill.
- [ ] **Step 5:** Dispatch prod when the deploy queue reads 0 (`gh run list --workflow deploy.yml --json status --jq '[.[]|select(.status!="completed")]|length'` → `0`; then `gh workflow run deploy.yml --ref main`); watch; founder check on `https://flickmeon.ai/chat`.

---

# Phase 1 — Message list feel

### Task 4: Extract `RoomMessageItem` (behaviour-preserving) and render collapsed messages

**Files:**
- Create: `apps/frontend/src/components/messaging/RoomMessageItem.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:319-383` (`renderMessage`)
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx` (add two cases; existing cases are the regression net)

**Interfaces:**
- Produces:
  ```tsx
  export interface RoomMessageItemProps {
    id: string;                                  // DOM id (RoomPanel keeps `${id}-${scope}-${messageId}`)
    message: RoomMessage;
    participant?: RoomParticipant;
    currentUserId: string;
    actionable: boolean;
    inThread: boolean;
    collapsed: boolean;
    replyCount: number;
    highlighted: boolean;                        // search target tint (existing bg-accent behaviour)
    markdown: Partial<Components>;               // roomMarkdown from RoomPanel
    reactionPending: boolean;
    reactionError: boolean;
    onToggleReaction?: (emoji: string) => void;
    onOpenThread?: (button: HTMLButtonElement) => void;
    onSelectParticipant?: RoomPanelProps["onSelectParticipant"];
    onUpdateMessage?: RoomPanelProps["onUpdateMessage"];
    onDownloadAttachment?: RoomPanelProps["onDownloadAttachment"];
    toolbarExtras?: ReactNode;                   // Task 6/7 add picker + copy link here
  }
  export function RoomMessageItem(props: RoomMessageItemProps): ReactElement;
  ```
  Collapsed rendering: no avatar, no byline; the left gutter shows the time (`HH:MM`) that is visually hidden until hover/focus-within (`.room-message-gutter`), and the article keeps `aria-label="Message from <name>"`.

- [ ] **Step 1: Write the failing tests** (append to `RoomPanel.test.tsx`)

```tsx
it("collapses consecutive messages from the same author and reveals the time in the gutter", () => {
  const second: RoomMessage = { ...message, messageId: "message-2", messageSeq: 2, createdAt: "2026-09-10T00:01:00Z", text: "Again" };
  render(<RoomPanel {...props} messages={[message, second]} />);
  const articles = screen.getAllByRole("article", { name: "Message from Friday" });
  expect(articles).toHaveLength(2);
  expect(within(articles[0]).getByText("Friday")).toBeInTheDocument();
  expect(within(articles[1]).queryByText("Friday")).not.toBeInTheDocument();
  expect(within(articles[1]).getByText(new Date(second.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))).toBeInTheDocument();
});

it("keeps every action reachable on a collapsed message", async () => {
  const user = userEvent.setup();
  const second: RoomMessage = { ...message, messageId: "message-2", messageSeq: 2, createdAt: "2026-09-10T00:01:00Z", text: "Again" };
  render(<RoomPanel {...props} messages={[message, second]} onSetReaction={vi.fn(async () => {})} onSelectThread={vi.fn()} />);
  const collapsed = screen.getAllByRole("article", { name: "Message from Friday" })[1];
  await user.click(within(collapsed).getByRole("button", { name: "Reply in thread" }));
  expect(within(collapsed).getByRole("button", { name: "Add reaction" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomPanel.test.tsx -t "collapses|collapsed"`
Expected: FAIL — second article still shows "Friday".

- [ ] **Step 3: Create `RoomMessageItem.tsx`** — move the body of `renderMessage` verbatim (lines 320-382) into the component, replacing closures with props:

```tsx
// apps/frontend/src/components/messaging/RoomMessageItem.tsx
"use client";

import type { ReactElement, ReactNode } from "react";
import { MessageCircle, SmilePlus } from "lucide-react";
import type { Components } from "react-markdown";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { cn } from "@/lib/utils";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { ParticipantProfile } from "./ParticipantProfile";
import { RoomImageAttachments } from "./RoomImageAttachments";
import { RoomMessageContent } from "./RoomMessageContent";
import type { RoomMessage, RoomParticipant, WorkState } from "./types";
import type { RoomPanelProps } from "./RoomPanel";

const reactionEmojis = ["👍", "❤️", "😂", "🎉", "👀", "✅"] as const; // replaced by the picker in Task 6

export interface RoomMessageItemProps {
  id: string;
  message: RoomMessage;
  participant?: RoomParticipant;
  currentUserId: string;
  actionable: boolean;
  inThread: boolean;
  collapsed: boolean;
  replyCount: number;
  highlighted: boolean;
  markdown: Partial<Components>;
  workStatus: (state: WorkState) => ReactNode;
  reactionPending: boolean;
  reactionError: boolean;
  onToggleReaction?: (emoji: string) => void;
  onOpenThread?: (button: HTMLButtonElement) => void;
  onSelectParticipant?: RoomPanelProps["onSelectParticipant"];
  onUpdateMessage?: RoomPanelProps["onUpdateMessage"];
  onDownloadAttachment?: RoomPanelProps["onDownloadAttachment"];
  toolbarExtras?: ReactNode;
}

export function RoomMessageItem({ id, message, participant, currentUserId, actionable, inThread, collapsed, replyCount, highlighted, markdown, workStatus, reactionPending, reactionError, onToggleReaction, onOpenThread, onSelectParticipant, onUpdateMessage, onDownloadAttachment, toolbarExtras }: RoomMessageItemProps): ReactElement {
  const name = participant?.displayName || message.author.displayName || "Unknown author";
  const timestamp = !Number.isNaN(Date.parse(message.createdAt)) ? message.createdAt : undefined;
  const time = timestamp ? new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  const appliedReactions = reactionEmojis.filter((emoji) => (message.reactions?.[emoji]?.length ?? 0) > 0);
  const actions = ((!message.deletedAt && onToggleReaction) || !inThread || toolbarExtras) ? <>
    {!message.deletedAt && onToggleReaction && <DropdownMenu>
      <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon-sm" aria-label="Add reaction" title="Add reaction" disabled={!actionable || reactionPending}><SmilePlus aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end" aria-label="Choose a reaction" onEscapeKeyDown={(event) => event.stopPropagation()}>
        {reactionEmojis.map((emoji) => <DropdownMenuItem key={emoji} disabled={!actionable || reactionPending}
          aria-label={`${message.reactions?.[emoji]?.includes(currentUserId) ? "Remove" : "Add"} ${emoji} reaction`}
          onSelect={() => onToggleReaction(emoji)}><span aria-hidden="true">{emoji}</span><span>{message.reactions?.[emoji]?.includes(currentUserId) ? "Remove" : "Add"} reaction</span></DropdownMenuItem>)}
      </DropdownMenuContent>
    </DropdownMenu>}
    {!inThread && onOpenThread && <Button type="button" variant="ghost" size="icon-sm" aria-label="Reply in thread" title="Reply in thread" disabled={!actionable}
      onClick={(event) => onOpenThread(event.currentTarget)}><MessageCircle aria-hidden="true" /></Button>}
    {toolbarExtras}
  </> : undefined;
  return <article id={id} tabIndex={-1} className={cn("room-message", collapsed && "room-message-collapsed", highlighted && "bg-accent")} aria-label={`Message from ${name}`}>
    {collapsed
      ? <span className="room-message-gutter" aria-hidden="true">{time}</span>
      : participant ? <ParticipantAvatar participant={participant} /> : <Identity name={name} avatarOnly size="default" />}
    <div className="room-message-content">
      {!collapsed && <div className="room-message-byline">{participant
        ? <ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} />
        : <span className="font-semibold truncate" title={name}>{name}</span>}
        {message.author.kind === "agent" && <span className="room-agent-label">Agent</span>}
        {timestamp && <time className="text-sm text-muted-foreground" dateTime={timestamp} title={new Date(timestamp).toLocaleString()}>{time}</time>}
      </div>}
      <RoomMessageContent message={message} currentUserId={currentUserId} actionable={actionable} onUpdate={onUpdateMessage} actions={actions}>
        <MarkdownView skipHtml components={markdown}>{message.text}</MarkdownView>
      </RoomMessageContent>
      {!message.deletedAt && onToggleReaction && appliedReactions.length > 0 && <div className="flex flex-wrap gap-1 mt-1" aria-label="Message reactions">
        {appliedReactions.map((emoji) => {
          const users = message.reactions?.[emoji] ?? [];
          const selected = users.includes(currentUserId);
          return <Button key={emoji} type="button" size="sm" variant={selected ? "secondary" : "ghost"}
            aria-label={`${selected ? "Remove" : "Add"} ${emoji} reaction${users.length ? `, ${users.length}` : ""}`} aria-pressed={selected}
            disabled={!actionable || reactionPending} onClick={() => onToggleReaction(emoji)}>{emoji}<span>{users.length}</span></Button>;
        })}
      </div>}
      {reactionError && <p role="alert" className="text-sm text-err">Could not update reaction. Try again.</p>}
      {!message.deletedAt && !!message.attachments?.length && onDownloadAttachment && <RoomImageAttachments roomId={message.roomId} attachments={message.attachments} download={onDownloadAttachment} />}
      {message.run && <div className="room-run">{workStatus(message.run.state)}
        {message.run.details && <details><summary>Run details</summary><pre>{message.run.details.slice(0, 8192)}</pre>{message.run.details.length > 8192 && <p className="text-sm text-muted-foreground">Further details omitted.</p>}</details>}
      </div>}
      {!inThread && replyCount > 0 && onOpenThread && <Button type="button" variant="ghost" className="room-reply" disabled={!actionable}
        aria-label={`Reply in thread, ${replyCount} ${replyCount === 1 ? "reply" : "replies"}`}
        onClick={(event) => onOpenThread(event.currentTarget)}>{replyCount} {replyCount === 1 ? "reply" : "replies"}</Button>}
    </div>
  </article>;
}
```

The `contextIds` block (dead scaffolding — no producer) is intentionally dropped here; delete the `RoomContext`-based `contextIds` rendering from `RoomPanel` too and leave the `contexts` prop/type untouched (the detail aside still renders `selectedContext` — do not touch that).

- [ ] **Step 4: Replace `renderMessage` in `RoomPanel.tsx`** with:

```tsx
function renderMessage(message: RoomMessage, inThread = false, collapsed = false) {
  const participant = participants.find((member) => member.kind === message.author.kind && member.id === message.author.id);
  return <RoomMessageItem key={message.messageId} id={`${id}-${inThread ? "thread" : "main"}-${message.messageId}`}
    message={message} participant={participant} currentUserId={currentUserId} actionable={actionable} inThread={inThread} collapsed={collapsed}
    replyCount={inThread ? 0 : messages.filter((candidate) => candidate.threadRootMessageId === message.messageId).length}
    highlighted={searchTarget?.messageId === message.messageId} markdown={roomMarkdown} workStatus={(state) => <WorkStatus state={state} />}
    reactionPending={reactionPending !== null} reactionError={reactionError === message.messageId}
    onToggleReaction={onSetReaction && !message.deletedAt ? (emoji) => void toggleReaction(message, emoji) : undefined}
    onOpenThread={(button) => openDetail("thread", message.messageId, button)}
    onSelectParticipant={onSelectParticipant} onUpdateMessage={onUpdateMessage} onDownloadAttachment={onDownloadAttachment} />;
}
async function toggleReaction(message: RoomMessage, emoji: string) {
  if (!onSetReaction || !actionable || reactionPending !== null) return;
  setReactionPending(message.messageId);
  setReactionError(null);
  try { await onSetReaction(message.messageId, emoji, !message.reactions?.[emoji]?.includes(currentUserId)); }
  catch { setReactionError(message.messageId); }
  finally { setReactionPending(null); }
}
```
Remove the now-unused imports from `RoomPanel.tsx` (`SmilePlus`, `MessageCircle` if unused, `RoomImageAttachments`, `RoomMessageContent`, `DropdownMenu*`, `FileText`) and the `reactionEmojis` constant. Thread rendering (`renderMessage(parentMessage, true)` and the replies map) passes `collapsed=false` — apply `buildTimeline` to thread replies too: `buildTimeline(replies, { readSeq: null }).map((item) => item.kind === "message" ? renderMessage(item.message, true, item.collapsed) : null)` (dividers are not shown in threads).

- [ ] **Step 5: Styles** — append to `messaging.css`:

```css
.room-message-collapsed { padding-top: 2px; padding-bottom: 2px; }
.room-message-gutter { flex: none; width: 40px; padding-top: 4px; font-size: .75rem; line-height: 1.5; text-align: right; color: var(--muted-foreground); font-variant-numeric: tabular-nums; opacity: 0; }
.room-message:hover .room-message-gutter, .room-message:focus-within .room-message-gutter { opacity: 1; }
@media (hover: none), (pointer: coarse) { .room-message-gutter { opacity: 1; } }
```
(Avatar column is 40px wide in `ParticipantAvatar` default size; if it is not, match the gutter width to the avatar width so collapsed text aligns with the byline text.)

- [ ] **Step 6: Run the full messaging suite**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS incl. the two new cases; scan `0`.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/messaging/RoomMessageItem.tsx apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx
git commit -m "refactor(messaging): extract RoomMessageItem and render grouped messages"
```

---

### Task 5: Mention chips

**Files:**
- Create: `apps/frontend/src/components/messaging/mentions.ts`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx` (`roomMarkdown.a`, and pass linkified text to `RoomMessageItem`)
- Modify: `apps/frontend/src/components/messaging/RoomMessageItem.tsx` (accept `text` override)
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/mentions.test.ts`, one case in `RoomPanel.test.tsx`

**Interfaces:**
- Produces:
  ```ts
  export const MENTION_SCHEME = "mention:";
  export function linkifyMentions(text: string, mentions: readonly MemberRef[], participants: readonly Pick<RoomParticipant, "kind" | "id" | "displayName" | "mentionHandle">[]): string;
  export function parseMentionHref(href: string): { kind: "human" | "agent"; id: string } | null;
  ```
  Rule: for each participant that is either in `mentions[]` or whose token appears in the text — agents match `@<mentionHandle>` (case-insensitive), humans match `@<displayName>` (exact, case-insensitive, longest names first) — replace whole-word occurrences with `[@Label](mention:<kind>:<id>)`. Skip fenced code blocks and inline code (split on backtick runs; only rewrite even-indexed segments). Never rewrite inside an existing markdown link `[…](…)`: simplest correct approach — skip segments already inside `](` … `)` by also splitting on that pattern is over-engineering; instead rewrite tokens only when preceded by start-of-string or whitespace/punctuation and not followed by `](`.

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/components/messaging/__tests__/mentions.test.ts
import { describe, expect, it } from "vitest";
import { linkifyMentions, parseMentionHref } from "../mentions";

const participants = [
  { kind: "agent" as const, id: "a1", displayName: "Friday", mentionHandle: "friday" },
  { kind: "human" as const, id: "u1", displayName: "Aden Kim", mentionHandle: null },
  { kind: "human" as const, id: "u2", displayName: "Aden", mentionHandle: null },
];

describe("linkifyMentions", () => {
  it("links agent handles and human display names, longest first", () => {
    expect(linkifyMentions("hey @friday and @Aden Kim, also @aden", [], participants))
      .toBe("hey [@friday](mention:agent:a1) and [@Aden Kim](mention:human:u1), also [@Aden](mention:human:u2)");
  });
  it("leaves code spans and fenced blocks alone", () => {
    expect(linkifyMentions("`@friday` stays\n```\n@friday\n```\n@friday goes", [], participants))
      .toBe("`@friday` stays\n```\n@friday\n```\n[@friday](mention:agent:a1) goes");
  });
  it("does not link partial words or unknown handles", () => {
    expect(linkifyMentions("email@friday.ai @nobody @fridays", [], participants)).toBe("email@friday.ai @nobody @fridays");
  });
  it("links a structured mention even when the text token differs in case", () => {
    expect(linkifyMentions("@FRIDAY?", [{ kind: "agent", id: "a1" }], participants)).toBe("[@FRIDAY](mention:agent:a1)?");
  });
});

describe("parseMentionHref", () => {
  it("parses and rejects", () => {
    expect(parseMentionHref("mention:agent:a1")).toEqual({ kind: "agent", id: "a1" });
    expect(parseMentionHref("https://x")).toBeNull();
    expect(parseMentionHref("mention:robot:a1")).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/mentions.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `mentions.ts`**

```ts
// apps/frontend/src/components/messaging/mentions.ts
import type { MemberRef } from "@lightbulb/shared";
import type { RoomParticipant } from "./types";

export const MENTION_SCHEME = "mention:";
type Target = Pick<RoomParticipant, "kind" | "id" | "displayName" | "mentionHandle">;

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export function parseMentionHref(href: string): { kind: "human" | "agent"; id: string } | null {
  if (!href.startsWith(MENTION_SCHEME)) return null;
  const [kind, ...rest] = href.slice(MENTION_SCHEME.length).split(":");
  const id = rest.join(":");
  return (kind === "human" || kind === "agent") && id ? { kind, id } : null;
}

export function linkifyMentions(text: string, mentions: readonly MemberRef[], participants: readonly Target[]): string {
  const tokens = participants
    .map((participant) => ({ participant, token: participant.kind === "agent" ? participant.mentionHandle : participant.displayName }))
    .filter((entry): entry is { participant: Target; token: string } => !!entry.token)
    .sort((left, right) => right.token.length - left.token.length);
  if (tokens.length === 0) return text;
  const pattern = new RegExp(`(^|[\\s(])@(${tokens.map((entry) => escapeRegExp(entry.token)).join("|")})(?![\\w.-]|\\]\\()`, "gi");
  const rewrite = (segment: string) => segment.replace(pattern, (match, lead: string, token: string) => {
    const entry = tokens.find((candidate) => candidate.token.toLowerCase() === token.toLowerCase());
    if (!entry) return match;
    const structured = mentions.some((mention) => mention.kind === entry.participant.kind && mention.id === entry.participant.id);
    void structured; // ponytail: text tokens that match a member are linked whether or not they are structured; the composer resolves them on send.
    return `${lead}[@${token}](${MENTION_SCHEME}${entry.participant.kind}:${entry.participant.id})`;
  });
  // Even segments are prose; odd segments are code (inline `…` or fenced ```…```).
  return text.split(/(```[\s\S]*?```|`[^`\n]*`)/).map((segment, index) => index % 2 === 0 ? rewrite(segment) : segment).join("");
}
```

- [ ] **Step 4: Render chips** — in `RoomPanel.tsx` change `roomMarkdown.a`:

```tsx
a: ({ href, children }) => {
  const mention = href ? parseMentionHref(href) : null;
  if (mention) return <span className="room-mention" data-kind={mention.kind}>{children}</span>;
  const safe = safeHref(href);
  return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{children}</a> : <span>{children}</span>;
},
```
In `RoomMessageItem`, add prop `text: string` and render `<MarkdownView skipHtml components={markdown}>{text}</MarkdownView>`; in `RoomPanel.renderMessage` pass `text={linkifyMentions(message.text, message.mentions, participants)}`. Own-mention highlight: in `renderMessage` compute `const mentionsMe = message.mentions.some((m) => m.kind === "human" && m.id === currentUserId)` and pass `highlighted={searchTarget?.messageId === message.messageId}` plus a new prop `mentionsViewer={mentionsMe}` that adds class `room-message-mentions-me`.

CSS:
```css
.room-mention { display: inline-block; padding: 0 4px; border-radius: 4px; font-weight: 600; color: var(--info); background: color-mix(in srgb, var(--info) 12%, transparent); }
.room-message-mentions-me { background: color-mix(in srgb, var(--brass) 10%, transparent); border-radius: 6px; }
```

- [ ] **Step 5: RoomPanel test** (append):

```tsx
it("renders @mentions as chips", () => {
  render(<RoomPanel {...props} messages={[{ ...message, text: "ping @friday" }]} />);
  expect(screen.getByText("@friday")).toHaveClass("room-mention");
});
```

- [ ] **Step 6: Run and commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; scan `0`.
```bash
git add apps/frontend/src/components/messaging/mentions.ts apps/frontend/src/components/messaging/__tests__/mentions.test.ts apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/RoomMessageItem.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx
git commit -m "feat(messaging): render mentions as chips and highlight messages that mention you"
```

---

### Task 6: Emoji picker, any-emoji reaction chips, quick reactions

**Files:**
- Create: `apps/frontend/src/components/messaging/emojiData.ts`, `apps/frontend/src/components/messaging/EmojiPicker.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomMessageItem.tsx` (replace the six-item dropdown; chips for every emoji present; quick reactions)
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/EmojiPicker.test.tsx`, cases in `RoomPanel.test.tsx`

**Interfaces:**
- `emojiData.ts` produces `export interface EmojiEntry { emoji: string; name: string; keywords: readonly string[] }` and `export const EMOJI_CATEGORIES: readonly { name: string; entries: readonly EmojiEntry[] }[]` — six categories (Smileys, People, Nature, Food, Objects, Symbols), ~400 entries total, hand-written, no dependency. Also `export function searchEmoji(query: string): EmojiEntry[]` (name/keyword `includes`, max 48).
- `EmojiPicker.tsx` produces:
  ```tsx
  export const RECENT_EMOJI_KEY = "lightbulb.messaging.recentEmoji";
  export function readRecentEmoji(): string[];           // try/catch localStorage, max 8
  export function recordRecentEmoji(emoji: string): void; // MRU, max 8
  export interface EmojiPickerProps { onPick: (emoji: string) => void; disabled?: boolean; triggerLabel?: string; }
  export function EmojiPicker(props: EmojiPickerProps): ReactElement; // Popover with a trigger button (SmilePlus icon)
  ```
  Behaviour: trigger opens a `Popover`; search input autofocused; "Recent" row when non-empty; category headings + 8-column grid of `<button aria-label={name}>`; arrow keys move within the grid, Enter picks, Escape closes and returns focus to the trigger; picking calls `onPick`, records recent, closes.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/frontend/src/components/messaging/__tests__/EmojiPicker.test.tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { EmojiPicker, RECENT_EMOJI_KEY, readRecentEmoji, recordRecentEmoji } from "../EmojiPicker";
import { searchEmoji } from "../emojiData";

beforeEach(() => localStorage.clear());

it("searches by name and keyword", () => {
  expect(searchEmoji("thumbs").some((entry) => entry.emoji === "👍")).toBe(true);
  expect(searchEmoji("party").some((entry) => entry.emoji === "🎉")).toBe(true);
  expect(searchEmoji("zzzznotanemoji")).toEqual([]);
});

it("keeps an MRU of eight recent emoji and survives a broken localStorage", () => {
  for (const emoji of ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]) recordRecentEmoji(emoji);
  expect(readRecentEmoji()).toEqual(["9️⃣", "8️⃣", "7️⃣", "6️⃣", "5️⃣", "4️⃣", "3️⃣", "2️⃣"]);
  localStorage.setItem(RECENT_EMOJI_KEY, "{not json");
  expect(readRecentEmoji()).toEqual([]);
});

it("opens, searches, picks with keyboard, records recent, and restores focus", async () => {
  const user = userEvent.setup();
  const onPick = vi.fn();
  render(<EmojiPicker onPick={onPick} />);
  const trigger = screen.getByRole("button", { name: "Add reaction" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Choose an emoji" });
  const search = within(dialog).getByRole("searchbox", { name: "Search emoji" });
  expect(search).toHaveFocus();
  await user.type(search, "thumbs up");
  await user.keyboard("{ArrowDown}{Enter}");
  expect(onPick).toHaveBeenCalledWith("👍");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(readRecentEmoji()).toEqual(["👍"]);
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/EmojiPicker.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write `emojiData.ts`** — six categories. Author the list by hand (every entry has `name` and 1–4 keywords). Minimum content per category: Smileys 80, People 60, Nature 60, Food 50, Objects 80, Symbols 70. Start with:

```ts
// apps/frontend/src/components/messaging/emojiData.ts
export interface EmojiEntry { emoji: string; name: string; keywords: readonly string[] }
const e = (emoji: string, name: string, ...keywords: string[]): EmojiEntry => ({ emoji, name, keywords });
export const EMOJI_CATEGORIES: readonly { name: string; entries: readonly EmojiEntry[] }[] = [
  { name: "Smileys", entries: [
    e("😀", "grinning face", "smile", "happy"), e("😂", "face with tears of joy", "laugh", "lol"), e("😅", "grinning face with sweat", "phew"),
    e("🙂", "slightly smiling face", "smile"), e("😉", "winking face", "wink"), e("😍", "smiling face with heart eyes", "love"),
    e("🤔", "thinking face", "hmm", "think"), e("😬", "grimacing face", "awkward"), e("🙃", "upside down face", "silly"),
    e("😎", "smiling face with sunglasses", "cool"), e("🥳", "partying face", "party", "celebrate"), e("😭", "loudly crying face", "cry", "sad"),
    // … continue to ≥80 entries
  ]},
  { name: "People", entries: [ e("👍", "thumbs up", "like", "yes", "approve"), e("👎", "thumbs down", "no", "dislike"), e("👀", "eyes", "look", "watching"),
    e("🙏", "folded hands", "thanks", "please"), e("👏", "clapping hands", "applause", "bravo"), e("🙌", "raising hands", "hooray"), e("💪", "flexed biceps", "strong"),
    e("🤝", "handshake", "deal", "agree"), e("👋", "waving hand", "hi", "bye"), e("✍️", "writing hand", "write"), /* … ≥60 */ ]},
  { name: "Nature", entries: [ e("🌱", "seedling", "plant", "grow"), e("🔥", "fire", "hot", "lit"), e("⭐", "star", "favorite"), e("🌈", "rainbow"), e("☀️", "sun", "sunny"), /* … ≥60 */ ]},
  { name: "Food", entries: [ e("☕", "hot beverage", "coffee"), e("🍕", "pizza"), e("🎂", "birthday cake", "cake"), /* … ≥50 */ ]},
  { name: "Objects", entries: [ e("💡", "light bulb", "idea", "lightbulb"), e("🚀", "rocket", "ship", "launch"), e("📌", "pushpin", "pin"), e("📎", "paperclip", "attach"),
    e("💰", "money bag", "money"), e("📈", "chart increasing", "growth", "up"), e("📉", "chart decreasing", "down"), e("🛠️", "hammer and wrench", "tools", "fix"), /* … ≥80 */ ]},
  { name: "Symbols", entries: [ e("✅", "check mark button", "done", "yes"), e("❌", "cross mark", "no", "wrong"), e("❤️", "red heart", "love"), e("🎉", "party popper", "party", "celebrate", "tada"),
    e("⚠️", "warning", "caution"), e("❓", "question mark", "question"), e("💯", "hundred points", "100"), e("🔁", "repeat", "retry"), /* … ≥70 */ ]},
];
export const ALL_EMOJI: readonly EmojiEntry[] = EMOJI_CATEGORIES.flatMap((category) => category.entries);
export function searchEmoji(query: string): EmojiEntry[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  return ALL_EMOJI.filter((entry) => entry.name.includes(needle) || entry.keywords.some((keyword) => keyword.includes(needle))).slice(0, 48);
}
```
The `/* … */` comments above mark where the implementer writes the remaining entries — the file must contain the real entries, not the comments, when committed. Verify counts with a test: `expect(ALL_EMOJI.length).toBeGreaterThanOrEqual(400)` and no duplicate `emoji` values.

- [ ] **Step 4: Write `EmojiPicker.tsx`**

```tsx
// apps/frontend/src/components/messaging/EmojiPicker.tsx
"use client";

import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactElement } from "react";
import { SmilePlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { EMOJI_CATEGORIES, searchEmoji, type EmojiEntry } from "./emojiData";
import "./messaging.css";

export const RECENT_EMOJI_KEY = "lightbulb.messaging.recentEmoji";
const RECENT_MAX = 8;
const COLUMNS = 8;

export function readRecentEmoji(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RECENT_EMOJI_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string").slice(0, RECENT_MAX) : [];
  } catch { return []; }
}

export function recordRecentEmoji(emoji: string): void {
  try { localStorage.setItem(RECENT_EMOJI_KEY, JSON.stringify([emoji, ...readRecentEmoji().filter((item) => item !== emoji)].slice(0, RECENT_MAX))); } catch { /* per-viewer convenience only */ }
}

export interface EmojiPickerProps { onPick: (emoji: string) => void; disabled?: boolean; triggerLabel?: string }

export function EmojiPicker({ onPick, disabled = false, triggerLabel = "Add reaction" }: EmojiPickerProps): ReactElement {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [recent, setRecent] = useState<string[]>([]);
  const grid = useRef<HTMLDivElement>(null);
  const results = useMemo(() => searchEmoji(query), [query]);
  const sections: { name: string; entries: readonly EmojiEntry[] }[] = query.trim()
    ? [{ name: "Results", entries: results }]
    : [...(recent.length ? [{ name: "Recent", entries: recent.map((emoji) => EMOJI_CATEGORIES.flatMap((c) => c.entries).find((entry) => entry.emoji === emoji) ?? { emoji, name: emoji, keywords: [] }) }] : []), ...EMOJI_CATEGORIES];

  function pick(emoji: string) {
    recordRecentEmoji(emoji);
    onPick(emoji);
    setOpen(false);
    setQuery("");
  }

  function onGridKey(event: KeyboardEvent<HTMLDivElement>) {
    const buttons = [...(grid.current?.querySelectorAll<HTMLButtonElement>("button[data-emoji]") ?? [])];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const move = (delta: number) => { event.preventDefault(); buttons[Math.max(0, Math.min(buttons.length - 1, index + delta))]?.focus(); };
    if (event.key === "ArrowRight") move(1);
    else if (event.key === "ArrowLeft") move(-1);
    else if (event.key === "ArrowDown") move(COLUMNS);
    else if (event.key === "ArrowUp") move(-COLUMNS);
  }

  return <Popover open={open} onOpenChange={(next) => { setOpen(next); if (next) setRecent(readRecentEmoji()); else setQuery(""); }}>
    <PopoverTrigger asChild><Button type="button" variant="ghost" size="icon-sm" aria-label={triggerLabel} title={triggerLabel} disabled={disabled}><SmilePlus aria-hidden="true" /></Button></PopoverTrigger>
    <PopoverContent align="end" className="room-emoji-picker" role="dialog" aria-label="Choose an emoji" onEscapeKeyDown={(event) => event.stopPropagation()}>
      <label className="sr-only" htmlFor={`${id}-search`}>Search emoji</label>
      <input id={`${id}-search`} type="search" className="room-input" placeholder="Search emoji" autoFocus value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => { if (event.key === "ArrowDown") { event.preventDefault(); grid.current?.querySelector<HTMLButtonElement>("button[data-emoji]")?.focus(); } }} />
      <div ref={grid} className="room-emoji-grid" onKeyDown={onGridKey}>
        {sections.map((section) => <section key={section.name} aria-label={section.name}>
          <h4 className="room-emoji-heading">{section.name}</h4>
          <div className="room-emoji-row">{section.entries.map((entry) => <button key={`${section.name}:${entry.emoji}`} type="button" data-emoji={entry.emoji} aria-label={entry.name} title={entry.name}
            onClick={() => pick(entry.emoji)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(entry.emoji); } }}>{entry.emoji}</button>)}</div>
          {section.entries.length === 0 && <p className="text-sm text-muted-foreground">No matching emoji.</p>}
        </section>)}
      </div>
    </PopoverContent>
  </Popover>;
}
```

CSS:
```css
.room-emoji-picker { width: min(320px, calc(100vw - 32px)); padding: 8px; display: grid; gap: 8px; }
.room-emoji-grid { max-height: 280px; overflow-y: auto; overscroll-behavior: contain; display: grid; gap: 8px; }
.room-emoji-heading { font-size: .75rem; font-weight: 600; color: var(--muted-foreground); margin: 4px 0; }
.room-emoji-row { display: grid; grid-template-columns: repeat(8, 1fr); gap: 2px; }
.room-emoji-row button { height: 32px; border-radius: 4px; font-size: 1.125rem; line-height: 1; }
.room-emoji-row button:hover, .room-emoji-row button:focus-visible { background: var(--accent); }
.room-quick-reactions { display: inline-flex; gap: 2px; }
.room-quick-reactions button { min-width: 28px; }
```
(Popover already gets the existing focus-visible outline from `.room-panel :is(button, …)` only when inside `.room-panel`; Radix portals the content to `body`, so add `.room-emoji-picker :is(button, input):focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }`.)

- [ ] **Step 5: Use it in `RoomMessageItem`** — replace the `DropdownMenu` block with:

```tsx
{!message.deletedAt && onToggleReaction && <>
  <span className="room-quick-reactions" aria-label="Quick reactions">{quickReactions.map((emoji) => <Button key={emoji} type="button" variant="ghost" size="icon-sm" aria-label={`React ${emoji}`} title={`React ${emoji}`} disabled={!actionable || reactionPending} onClick={() => onToggleReaction(emoji)}>{emoji}</Button>)}</span>
  <EmojiPicker onPick={onToggleReaction} disabled={!actionable || reactionPending} />
</>}
```
with `const quickReactions = (readRecentEmoji().length ? readRecentEmoji() : ["👍", "✅", "🎉", "👀"]).slice(0, 4);` computed once per render (cheap, localStorage read). Reaction chips: replace `appliedReactions` with `Object.entries(message.reactions ?? {}).filter(([, users]) => users.length > 0)` so any emoji renders; delete the `reactionEmojis` constant.

- [ ] **Step 6: RoomPanel tests** (append):

```tsx
it("shows quick reactions and a picker in the toolbar and renders any applied emoji as a chip", async () => {
  const user = userEvent.setup();
  const onSetReaction = vi.fn(async () => {});
  render(<RoomPanel {...props} messages={[{ ...message, reactions: { "🚀": ["owner"], "🙏": ["someone"] } }]} onSetReaction={onSetReaction} />);
  expect(screen.getByRole("button", { name: "Remove 🚀 reaction, 1" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Add 🙏 reaction, 1" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "React 👍" }));
  expect(onSetReaction).toHaveBeenCalledWith("message", "👍", true);
  await user.click(screen.getByRole("button", { name: "Add reaction" }));
  expect(screen.getByRole("dialog", { name: "Choose an emoji" })).toBeInTheDocument();
});
```

- [ ] **Step 7: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.
```bash
git add apps/frontend/src/components/messaging/emojiData.ts apps/frontend/src/components/messaging/EmojiPicker.tsx apps/frontend/src/components/messaging/RoomMessageItem.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/EmojiPicker.test.tsx apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx
git commit -m "feat(messaging): emoji picker, quick reactions, and any-emoji reaction chips"
```

---

### Task 7: Copy link and permalinks

**Files:**
- Create: `apps/frontend/src/components/messaging/messageLinks.ts`
- Modify: `apps/frontend/src/components/workspace/panelRegistry.ts:100` (`ONE_SHOT_PARAMS`), `:193-201` (chat descriptor), `:302-310` (`durableParamsFor`)
- Modify: `apps/frontend/src/components/workspace/panels/ChatPanel.tsx`
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (expose `openMessage(roomId, messageId)`; pass `targetMessageId` to `roomProps`)
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx` (copy-link toolbar action via `toolbarExtras`)
- Test: `apps/frontend/src/components/messaging/__tests__/messageLinks.test.ts`, `apps/frontend/src/components/workspace/__tests__/urlChannel.test.ts` (add), `RoomPanel.test.tsx` (add)

**Interfaces:**
- `messageLinks.ts`:
  ```ts
  export function messagePermalink(roomId: string, messageId: string, origin = window.location.origin): string; // `${origin}/chat?panel=chat&room=${roomId}&message=${messageId}`
  export async function copyText(text: string): Promise<boolean>; // navigator.clipboard.writeText with try/catch → false
  ```
- `panelRegistry.describePanel("chat", { room, message })` → `{ panelId: "chat", component: "chat", params: { roomId, messageId? } }` when `room` is present and `agent` is not; `ONE_SHOT_PARAMS` gains `"message"`; `durableParamsFor("chat")` gains `"room"`. The existing `agent` form is unchanged.
- `ChatPanelParams` gains `roomId?: string; messageId?: string`; when `roomId` is present the panel calls `openMessage(roomId, messageId ?? null)`.
- Context: `openMessage: (roomId: string, messageId: string | null) => void` — `rooms.selectRoom(roomId)` then stores `{ roomId, messageId }` in state; `roomProps.targetMessageId` is that id while `rooms.activeRoomId === roomId`, cleared after 3s.

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/components/messaging/__tests__/messageLinks.test.ts
import { expect, it, vi } from "vitest";
import { copyText, messagePermalink } from "../messageLinks";

it("builds a chat permalink", () => {
  expect(messagePermalink("room-1", "msg-1", "https://flickmeon.ai")).toBe("https://flickmeon.ai/chat?panel=chat&room=room-1&message=msg-1");
});

it("copies via the clipboard API and reports failure without throwing", async () => {
  const writeText = vi.fn(async () => {});
  Object.assign(navigator, { clipboard: { writeText } });
  await expect(copyText("x")).resolves.toBe(true);
  writeText.mockRejectedValueOnce(new Error("denied"));
  await expect(copyText("x")).resolves.toBe(false);
});
```

Append to `apps/frontend/src/components/workspace/__tests__/urlChannel.test.ts` (read the file first to match its imports):
```ts
it("treats ?panel=chat&room=…&message=… as a chat command with a one-shot message", () => {
  const command = parseCommand("?panel=chat&room=r1&message=m1");
  expect(command?.descriptor.params).toMatchObject({ roomId: "r1" });
  expect(command?.oneShots).toEqual({ message: "m1" });
  expect(stripOneShots("?panel=chat&room=r1&message=m1")).toBe("panel=chat&room=r1");
});
```

RoomPanel (append):
```tsx
it("copies a message permalink from the toolbar", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn(async () => {});
  Object.assign(navigator, { clipboard: { writeText } });
  render(<RoomPanel {...props} />);
  await user.click(screen.getByRole("button", { name: "Copy link" }));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("panel=chat&room=room&message=message"));
  expect(await screen.findByRole("status", { name: "Link copied" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/messageLinks.test.ts src/components/workspace/__tests__/urlChannel.test.ts src/components/messaging/__tests__/RoomPanel.test.tsx -t "permalink|Copy link|one-shot message"`
Expected: FAIL.

- [ ] **Step 3: Implement**

`messageLinks.ts`:
```ts
export function messagePermalink(roomId: string, messageId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "chat", room: roomId, message: messageId });
  return `${origin}/chat?${params.toString()}`;
}
export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}
```

`panelRegistry.ts`: `export const ONE_SHOT_PARAMS = ["new", "newIssue", "action", "message"] as const;`. In the `key === "chat"` branch (line 193) before the `agentId` lookup:
```ts
if (!params.agent && !params.agentId && params.room) {
  return { panelId: "chat", component: "chat", title: "Messages", params: { roomId: params.room, ...(params.message ? { messageId: params.message } : {}) } };
}
```
(Match the exact `title`/`panelId` values the existing chat branch returns — read lines 193-201 and mirror them.) In `durableParamsFor` (line 302), add `"room"` to the chat list beside `"agent"`/`"agentId"`.

`ChatPanel.tsx`: extend `ChatPanelParams` with `roomId?: string; messageId?: string;`; pull `openMessage` from `useMessagingWorkspace()`; add
```tsx
const lastOpened = useRef<string | null>(null);
useEffect(() => {
  if (!params.roomId) { lastOpened.current = null; return; }
  const key = `${params.roomId}:${params.messageId ?? ""}`;
  if (lastOpened.current === key) return;
  lastOpened.current = key;
  openMessage(params.roomId, params.messageId ?? null);
}, [params.roomId, params.messageId, openMessage]);
```

`MessagingWorkspaceContext.tsx`: add `const [target, setTarget] = useState<{ roomId: string; messageId: string | null } | null>(null);` and
```tsx
const openMessage = useCallback((roomId: string, messageId: string | null) => { rooms.selectRoom(roomId); setTarget({ roomId, messageId }); }, [rooms.selectRoom]);
useEffect(() => { if (!target?.messageId) return; const timer = setTimeout(() => setTarget(null), 3000); return () => clearTimeout(timer); }, [target]);
```
pass `targetMessageId: target?.roomId === rooms.activeRoomId ? target.messageId : null,` in `roomProps`, and add `openMessage` to the context value + `MessagingWorkspaceValue` interface.

`RoomPanel.tsx`: add `const [copied, setCopied] = useState<string | null>(null);` and pass to `RoomMessageItem`:
```tsx
toolbarExtras={<Button type="button" variant="ghost" size="icon-sm" aria-label="Copy link" title="Copy link" onClick={async () => {
  const ok = await copyText(messagePermalink(roomId, message.messageId));
  setCopied(ok ? message.messageId : null);
  if (ok) setTimeout(() => setCopied((current) => current === message.messageId ? null : current), 2000);
}}><Link aria-hidden="true" /></Button>}
```
(`Link` from `lucide-react`.) Render `{copied && <p role="status" aria-label="Link copied" className="sr-only">Link copied</p>}` once in `RoomContent` (near the typing line) so screen readers announce it; the toolbar button's `title` flips to "Copied" while `copied === message.messageId`.

- [ ] **Step 4: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging src/components/workspace && pnpm lint src/components/messaging src/components/workspace && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.
```bash
git add apps/frontend/src/components/messaging/messageLinks.ts apps/frontend/src/components/messaging/__tests__/messageLinks.test.ts apps/frontend/src/components/workspace/panelRegistry.ts apps/frontend/src/components/workspace/__tests__/urlChannel.test.ts apps/frontend/src/components/workspace/panels/ChatPanel.tsx apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/RoomMessageItem.tsx
git commit -m "feat(messaging): copy message links and open permalinks"
```

---

### Task 8: Honest run status line, agent-facing docs, and Phase 1 ship

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:385-408` (`renderRun`)
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md` (Messages row)
- Test: `RoomPanel.test.tsx` (add)

**Behaviour:** a run in `runs` whose `state` is not `running` and whose `runId` matches no message (that is exactly what `useRooms.runs` returns — completed runs with a reply are already filtered out) renders as one muted line instead of the full run card: "Friday finished · no reply" (`completed`), "Friday stopped" (`aborted`), "Friday's run failed" (`failed`, keeps the `WorkStatus` icon in `text-err`). Tool traces stay behind `<details>` on that line.

- [ ] **Step 1: Test** (append to `RoomPanel.test.tsx`)

```tsx
it("collapses a finished run with no reply into a muted status line", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "completed", tools: [] };
  render(<RoomPanel {...props} runs={[run]} />);
  expect(screen.getByText("Friday finished · no reply")).toBeInTheDocument();
  expect(screen.queryByRole("article", { name: "Live run from Friday" })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify it fails** — `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomPanel.test.tsx -t "no reply"` → FAIL.

- [ ] **Step 3: Implement** — at the top of `renderRun`:

```tsx
if (run.state !== "running") {
  const agentName = agent?.displayName ?? "Agent";
  const label = run.state === "completed" ? `${agentName} finished · no reply` : run.state === "aborted" ? `${agentName} stopped` : `${agentName}'s run failed`;
  return <div key={`${run.roomId}:${run.agentId}:${run.runId}`} className={cn("room-run-line", run.state === "failed" ? "text-err" : "text-muted-foreground")} role="status">
    <Circle size={12} aria-hidden="true" />{label}
    {run.tools.length > 0 && <details><summary>What it did</summary><ToolActivityTrace calls={run.tools} /></details>}
  </div>;
}
```
CSS: `.room-run-line { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 4px 0 4px 52px; font-size: .875rem; } .room-run-line details { flex-basis: 100%; }`

- [ ] **Step 4: Update the agent-facing surfaces doc** — in `surfaces.md`, the Messages/chat row: add one sentence: "Each message has a hover toolbar (react with any emoji, reply in thread, copy link; your own messages also edit/delete). A message link looks like `flickmeon.ai/chat?panel=chat&room=<roomId>&message=<messageId>` and opens the room scrolled to that message." Update `MAINTENANCE.md` anchors if the sibling file lists this row.

- [ ] **Step 5: Run the full frontend suite, lint, scan, build**

Run: `cd apps/frontend && pnpm test && pnpm lint && node scripts/brand-tokens-scan.mjs src && pnpm run build`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx apps/infra/openclaw/skills/lightbulb/references/surfaces.md apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md
git commit -m "feat(messaging): muted status line for silent agent turns"
```

- [ ] **Step 7 (controller): review, screenshots, ship.** Impeccable `detect --json --target apps/frontend/src/components/messaging`; one screenshot round at 1440×900 and 390×844 on dev (long room, mentions, reactions, pill, thread); spawn `impeccable:impeccable-finish-reviewer` with the spec's "Message list" section as the contract; fix material findings in one batch; PR `feat(messaging): phase 1 — message list feel` (Tasks 4–8 may ship as two PRs: 4–5 and 6–8); merge; dev verify; prod dispatch when the queue reads 0; founder check on `https://flickmeon.ai/chat`.

---

## Self-review

- **Spec coverage (Phase 0):** DESIGN.md + baseline critique → Task 3; newest-on-open, auto-scroll, pill, own-send scroll → Task 2; delete Codex plan → Task 3. Backward history paging deferred per the spec.
- **Spec coverage (Phase 1):** emoji picker + quick reactions → Task 6; copy link → Task 7; grouping → Tasks 1, 4; date dividers → Tasks 1, 2; "New" line → Tasks 1, 2; hover timestamps → Task 4 (gutter); mention chips + own-mention highlight → Task 5; permalinks → Task 7; run status line → Task 8; separators' a11y roles → Task 2; keyboard reachability → existing toolbar (`focus-within`) + Task 6 grid keys; touch → shipped always-visible toolbar.
- **Placeholders:** the only `/* … */` markers are in Task 6 Step 3 and are explicitly "write the real entries" instructions with a count test.
- **Type consistency:** `renderMessage(message, inThread, collapsed)` (Task 2 → 4); `RoomMessageItemProps.toolbarExtras` (Task 4 → 7); `readSeqAtOpen` / `targetMessageId` on `RoomPanelProps` (Task 2 → 7); `openMessage` on the context (Task 7 only); `linkifyMentions` signature (Task 5).
