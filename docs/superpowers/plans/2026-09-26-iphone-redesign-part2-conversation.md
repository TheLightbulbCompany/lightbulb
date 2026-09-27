# iPhone Redesign Part 2 — Conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the room, thread and Claude Code / Codex session screens as Muse-style bubble conversations — mine ink-filled on the right, everyone else's wide pane bubbles on the left — with chips hanging off bubbles, a live working/typing bubble, real read state and labels on unprompted agent messages, keeping every existing room feature.

**Architecture:** One family of bubble primitives (`components/conversation/bubbles.tsx`) and one `ConversationHeader` are shared by `room/[roomId]`, its thread screen and `session/[sessionId]`. `MessageGroup`, `RunBlock`, `RoomComposer`, `TranscriptRow` and `SessionPromptRow` keep their exported names and call sites but render in the new language. Read-state derivation and the unprompted label are pure functions in `@lightbulb/shared` (`messaging/readState.ts`, vitest). Everything reads `useChatPalette()` from Part 1.

**Tech Stack:** Expo SDK 57 / React Native 0.86, expo-router native Stack headers, `react-native-markdown-display`, RN `Animated` via the existing `usePulse` (Reduce Motion aware), jest-expo + @testing-library/react-native, vitest.

**Spec:** `docs/superpowers/specs/2026-09-26-iphone-messaging-redesign-design.md` (binding). Visual reference only: `.superpowers/brainstorm/73529-1790476059/content/conversation-muse-v2.html`. **Depends on Part 1** (`chatPalette`/`useChatPalette`, `Avatar` `ring`/`vendor`/`palette`, `AvatarRing`, `otherMembers`) being merged first.

## Global Constraints

- **JS/TS only.** No new dependency, no native module, no change to `apps/mobile/app.json`, `apps/mobile/package.json` or `pnpm-lock.yaml`; the OTA fingerprint must not move (Task 12).
- **Tokens:** `useChatPalette()` only (Part 1 table, repeated for reference). Light / dark: `ground` `#EFEFEF` / `#101012` (conversation background), `surface` `#FFFFFF` / `#1E1E21` (others' bubbles), `edge` `#E2E2E2` / `#323236` (bubble stroke), `fg` `#1A1A1C` / `#ECECEE` (mine bubble fill, body text), `onFg` `#FFFFFF` / `#101012` (text in mine bubble), `fgSoft` `#52525A` / `#B4B4B8`, `fgMute` `#606069` / `#97979C` (metadata), `chip` `#E4E4E6` / `#27272B` (hanging chips), `raised` `#F7F7F7` / `#27272B` (code wells), `link` `#1E52CC` / `#8FB0FF`, `live` `#186D48` / `#4BC08A`, `alert` `#A83C29` / `#E7806F`, `warn` `#52525A` / `#B4B4B8`. No new colour literals outside `theme.ts`. Legacy-token mapping: see Part 1 Global Constraints (`ink`→`fg`, `mute`→`fgMute`, `forest` links→`link`, `forest` buttons→`fg`/`onFg`, `errorText`→`alert`, `green`→`live`, `codeBg`/`chip`→`raised`).
- **Bubbles:** mine — `fg` fill, `onFg` text, right-aligned, `maxWidth: "78%"`, radius 18, padding 8×12. Others (agents and humans) — `surface` fill, 1px `edge` stroke, on the `ground` background, left-aligned, `maxWidth: "94%"`, radius 18, padding 9×12; avatar (28pt) + name (12pt `fgMute`) on the first bubble of a run only; consecutive bubbles from one sender group without repeating either (the existing `buildTimeline` `collapsed` flag).
- **Hanging off a bubble, never bubbles themselves:** tool-run chip, sub-agent chip, "Thought" chip, reactions, reply count, timestamp, read state, unprompted label.
- **Fonts:** body 16pt `SourceSans3_400Regular` (headings `SourceSans3_600SemiBold`), metadata ≥ 12pt; nav chrome system font (`type.navTitle`/`type.navMeta`).
- **No monospace except code.** `fonts.mono` only for fenced/indented code blocks, inline `code` spans, tool output wells and diff lines (all code). The session TUI glyphs (`GLYPH`: ❯ ⏺ ⎿ ✻ in Menlo) are removed from the phone — they are the "broken icons".
- **Motion:** the dots pulse through `usePulse` (Reduce Motion → static dimmed dots). Native transitions only.
- **Accessibility:** every bubble carries a label (`${author}: ${text}` for others, `You: ${text}` for mine); chips are buttons with `accessibilityState.expanded`; 44pt targets via `hitSlop`; message text scales with Dynamic Type (no `maxFontSizeMultiplier` on bodies).
- **Read state (resolved against the data that exists):** there is **no "Delivered" for rooms** — clients receive neither the WORK consumer's acceptance nor other members' read cursors (`RoomSummary.lastReadSeq` is the viewer's own; `_room_summary_fields` returns only that). So a room message reads **Sent** → **Read** (a DM counterpart has read it) / **Read by N** (groups and channels), where "has read" = a run triggered by the message, or a later message from that member (an agent's only when it is a room-turn reply, `runId !== null`). Silent human readers are not counted. **Sessions** do have delivery: the follow-up receipt chip ("Sending", "Queued on your Mac", "Delivered", "Not delivered", "Delivery unknown") is shown as the read state under the pending bubble.
- **Unprompted label:** an agent message with `runId === null` (posted outside a room turn — heartbeat, routine, `lightbulb-post.sh`; backend `messaging.py` stamps `run_id` only on room-turn replies) gets the label **"on its own"**. The backend exposes no kind (nightly vs routine vs session-finished), so one honest label replaces the spec's three examples.
- **`@lightbulb/shared` stays framework-neutral**, with vitest tests for new logic.
- **Test commands (targeted only):** `cd apps/mobile && pnpm jest <paths>` (escape parentheses), `pnpm typecheck`, `pnpm lint`; `cd packages/shared && pnpm test <paths>`.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **Read state for a message nobody has answered yet in a group** — must read "Sent", never "Read by 0", and must not count the viewer's own later messages or a system line. Pinned in Task 2 (`readState` tests).
2. **An agent that posts a routine message after mine without having run on it** — must not flip my message to "Read". Pinned in Task 2 (`runId === null` agent message ignored).
3. **Deleted messages and local (unsent/in-flight) rows** — a deleted bubble shows "Message deleted", keeps no reactions and never carries the read state; an in-flight row reads "Sending…", a failed one "Not sent · tap to retry". Pinned in Task 5.
4. **Very long agent replies and wide code blocks** — the bubble caps at 94% width, code scrolls inside its well instead of widening the bubble. Pinned in Task 1 (fence wraps in a horizontal `ScrollView`) and checked on the simulator (Task 12).
5. **Dark mode inside the mine bubble** — links, mentions and inline code must stay legible on the `fg` fill (onFg text, fgSoft code chip). Pinned in Task 1 (`inverse` styles test).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/mobile/src/components/chat/MessageMarkdown.tsx` (modify) | `palette` / `inverse` tones, 16pt body, sans fence label, scrollable fences |
| `packages/shared/src/messaging/readState.ts` (create) + `.test.ts` | `readState`, `readStateLabel`, `lastOwnMessageId`, `unpromptedLabel` |
| `packages/shared/src/index.ts` (modify) | exports |
| `apps/mobile/src/components/conversation/bubbles.tsx` (create) + test | `Bubble`, `BubbleMeta`, `MetaText`, `Chip`, `WorkingBubble` |
| `apps/mobile/src/components/conversation/ConversationHeader.tsx` (create) + test | avatar + name + status line nav title |
| `apps/mobile/src/components/room/MessageGroup.tsx` (modify) + test | a canonical message as a bubble with its hanging meta |
| `apps/mobile/src/components/room/RunBlock.tsx` (modify) + test | live run as working bubble / streaming bubble + tool chip + steer/stop |
| `apps/mobile/src/components/room/RoomComposer.tsx` (modify) + test | "+" · pill field · send |
| `apps/mobile/src/app/(app)/room/[roomId]/index.tsx` (modify) + `room/__tests__/conversation.test.tsx` | the room screen |
| `apps/mobile/src/app/(app)/room/[roomId]/thread/[messageId].tsx` (modify) + `room/__tests__/thread.test.tsx` | the thread screen |
| `apps/mobile/src/components/session/TranscriptRow.tsx`, `SessionPromptRow.tsx` (modify) + test | session transcript in bubbles + chips, pill composer |
| `apps/mobile/src/app/(app)/session/[sessionId].tsx` (modify) + `__tests__/session.test.tsx` | session screen, details sheet, scroll-to-load older |

---

### Task 1: MessageMarkdown tones for bubbles

**Files:**
- Modify: `apps/mobile/src/components/chat/MessageMarkdown.tsx`
- Test: `apps/mobile/src/components/chat/__tests__/MessageMarkdown.tones.test.tsx`

**Interfaces:**
- Consumes: `ChatPalette`, `chatPalette` (Part 1).
- Produces: `MessageMarkdown({ text, palette?, inverse? }: { text: string; palette?: ChatPalette; inverse?: boolean })`. No `palette` → today's light styles, byte-for-byte (Teams run detail and every other legacy caller unchanged). With `palette`: body `fonts.sans` 16/22 `palette.fg` (or `palette.onFg` when `inverse`); headings `fonts.sansSemiBold` 18/17/16 in the same colour; links `palette.link` underlined (`inverse` → `palette.onFg` underlined); mentions `palette.link` on no fill (`inverse` → `palette.onFg`, semibold); inline code `fonts.mono` 14 on `palette.raised` with `palette.fg` (`inverse` → `palette.fgSoft` fill, `palette.onFg` text); code blocks/fences `fonts.mono` 13/19 on `palette.raised`, 1px `palette.edge`, radius 12; the fence language label switches to `fonts.sansMedium` 12 `palette.fgMute` (it is a label, not code); blockquote rule `palette.edge`, text `palette.fgSoft`; list bullets `palette.fg`. The fence body renders inside `<ScrollView horizontal showsHorizontalScrollIndicator={false}>` so wide code scrolls inside its well.
- Implementation shape: keep the existing `markdownStyles` object for the no-palette path; add `function bubbleStyles(p: ChatPalette, inverse: boolean)` returning the same keys, memoised in a module `WeakMap<ChatPalette, { plain; inverse }>` so a scroll never rebuilds styles.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/chat/__tests__/MessageMarkdown.tones.test.tsx
import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../config/theme";
import { MessageMarkdown } from "../MessageMarkdown";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

const color = (text: string) => StyleSheet.flatten(screen.getByText(text).props.style).color;

it("draws body text at 16pt in the palette ink", async () => {
  await render(<MessageMarkdown text="hello there" palette={chatPalette.dark} />);
  const style = StyleSheet.flatten(screen.getByText("hello there").props.style);
  expect(style.fontSize).toBe(16);
  expect(style.color).toBe(chatPalette.dark.fg);
});

it("inverts for a mine bubble, links and inline code included", async () => {
  await render(<MessageMarkdown text={"see [docs](https://x.dev) and `npm test`"} palette={chatPalette.light} inverse />);
  expect(color("docs")).toBe(chatPalette.light.onFg);
  const code = StyleSheet.flatten(screen.getByText("npm test").props.style);
  expect(code.backgroundColor).toBe(chatPalette.light.fgSoft);
  expect(code.color).toBe(chatPalette.light.onFg);
});

it("labels a fence in the sans face and keeps the code mono", async () => {
  await render(<MessageMarkdown text={"```bash\ngh pr view 3284\n```"} palette={chatPalette.light} />);
  expect(StyleSheet.flatten(screen.getByText("bash").props.style).fontFamily).toBe("SourceSans3_500Medium");
  expect(StyleSheet.flatten(screen.getByText("gh pr view 3284").props.style).fontFamily).toBe("Menlo");
});

it("keeps the legacy look when no palette is passed", async () => {
  await render(<MessageMarkdown text="legacy" />);
  expect(StyleSheet.flatten(screen.getByText("legacy").props.style).fontSize).toBe(15);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/components/chat/__tests__/MessageMarkdown.tones.test.tsx`
Expected: FAIL on the 16pt / inverse / fence-label assertions.

- [ ] **Step 3: Implement** per the Interfaces block (`MessageMarkdownInner({ text, palette, inverse = false })` picks `palette ? cached(palette, inverse) : markdownStyles`; the `fence` render rule wraps its body `Text` in the horizontal `ScrollView`).

- [ ] **Step 4: Run tests**

Run: `cd apps/mobile && pnpm jest src/components/chat/__tests__`
Expected: PASS (existing `MessageMarkdown.links.test.tsx` included).

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/chat/MessageMarkdown.tsx apps/mobile/src/components/chat/__tests__/MessageMarkdown.tones.test.tsx
git commit -m "feat(mobile): markdown tones for conversation bubbles

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Shared read state and unprompted label

**Files:**
- Create: `packages/shared/src/messaging/readState.ts`
- Create: `packages/shared/src/messaging/readState.test.ts`
- Modify: `packages/shared/src/index.ts` (next to the Part 1 `chatList` exports)

**Interfaces:**
- Produces:
  ```ts
  export type ReadState = { kind: "sent" } | { kind: "read" } | { kind: "read-by"; count: number };
  export function readState(input: {
    message: Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "runId">;
    messages: readonly Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "runId">[];
    runs: readonly { triggerMessageId?: string | null; agentId: string }[];
    members: readonly Pick<RoomMember, "kind" | "id">[];
    viewerId: string;
  }): ReadState;
  export function readStateLabel(state: ReadState): string;           // "Sent" | "Read" | "Read by N"
  export function lastOwnMessageId(messages: readonly Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "deletedAt">[], viewerId: string): string | null;
  export const UNPROMPTED_LABEL = "on its own";
  export function unpromptedLabel(message: Pick<CanonicalRoomMessage, "author" | "runId" | "deletedAt">): string | null;
  ```

- [ ] **Step 1: Write the failing test**

```ts
// packages/shared/src/messaging/readState.test.ts
import { describe, expect, it } from "vitest";

import { lastOwnMessageId, readState, readStateLabel, unpromptedLabel } from "./readState";
import type { CanonicalRoomMessage } from "./types";

type M = Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "runId" | "deletedAt">;
const msg = (seq: number, kind: "human" | "agent" | "system", id: string, runId: string | null = null, deletedAt: string | null = null): M => ({
  messageId: `m${seq}`, messageSeq: seq, author: { kind, id, displayName: id }, runId, deletedAt,
});
const me = { kind: "human" as const, id: "me" };
const ceo = { kind: "agent" as const, id: "ceo" };
const leo = { kind: "human" as const, id: "leo" };
const eng = { kind: "agent" as const, id: "eng" };

describe("readState", () => {
  const mine = msg(5, "human", "me");

  it("is Sent until someone shows they read it", () => {
    expect(readState({ message: mine, messages: [mine], runs: [], members: [me, ceo], viewerId: "me" })).toEqual({ kind: "sent" });
  });

  it("is Read in a DM once the agent's run for it started", () => {
    expect(readState({ message: mine, messages: [mine], runs: [{ triggerMessageId: "m5", agentId: "ceo" }], members: [me, ceo], viewerId: "me" }))
      .toEqual({ kind: "read" });
  });

  it("is Read in a DM once the counterpart replied after it", () => {
    const reply = msg(6, "human", "leo");
    expect(readState({ message: mine, messages: [mine, reply], runs: [], members: [me, leo], viewerId: "me" })).toEqual({ kind: "read" });
  });

  it("counts distinct readers in a group", () => {
    const messages = [mine, msg(6, "agent", "ceo", "run-1"), msg(7, "human", "leo"), msg(8, "human", "leo")];
    expect(readState({ message: mine, messages, runs: [{ triggerMessageId: "m5", agentId: "eng" }], members: [me, ceo, leo, eng], viewerId: "me" }))
      .toEqual({ kind: "read-by", count: 3 });
  });

  it("ignores an agent post made outside a room turn", () => {
    const routine = msg(6, "agent", "ceo", null);
    expect(readState({ message: mine, messages: [mine, routine], runs: [], members: [me, ceo], viewerId: "me" })).toEqual({ kind: "sent" });
  });

  it("ignores my own later messages, system lines, earlier messages and non-members", () => {
    const messages = [msg(4, "human", "leo"), mine, msg(6, "human", "me"), msg(7, "system", "sys"), msg(8, "human", "gone")];
    expect(readState({ message: mine, messages, runs: [{ triggerMessageId: "m9", agentId: "ceo" }], members: [me, ceo, leo], viewerId: "me" }))
      .toEqual({ kind: "sent" });
  });
});

describe("readStateLabel", () => {
  it("words each state", () => {
    expect(readStateLabel({ kind: "sent" })).toBe("Sent");
    expect(readStateLabel({ kind: "read" })).toBe("Read");
    expect(readStateLabel({ kind: "read-by", count: 2 })).toBe("Read by 2");
  });
});

describe("lastOwnMessageId", () => {
  it("finds my newest live canonical message", () => {
    const list = [msg(1, "human", "me"), msg(2, "agent", "ceo", "r"), msg(3, "human", "me", null, "2026-09-26T00:00:00Z"), msg(0, "human", "me")];
    expect(lastOwnMessageId(list, "me")).toBe("m1");
    expect(lastOwnMessageId([], "me")).toBeNull();
  });
});

describe("unpromptedLabel", () => {
  it("labels an agent message posted outside a room turn, and nothing else", () => {
    expect(unpromptedLabel(msg(1, "agent", "ceo", null))).toBe("on its own");
    expect(unpromptedLabel(msg(1, "agent", "ceo", "run-1"))).toBeNull();
    expect(unpromptedLabel(msg(1, "human", "leo"))).toBeNull();
    expect(unpromptedLabel(msg(1, "agent", "ceo", null, "2026-09-26T00:00:00Z"))).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd packages/shared && pnpm test src/messaging/readState.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// packages/shared/src/messaging/readState.ts
/**
 * "Sent" / "Read" / "Read by N" under the viewer's last message, from data the
 * client already holds. There is deliberately NO "Delivered" for rooms: the
 * backend exposes neither an agent's ingress acceptance nor any other member's
 * read cursor (a room summary's `lastReadSeq` is the viewer's own). A member
 * has read a message once a run it triggered exists (live or seeded from the
 * ledger) or once that member posted after it — an agent only by a room-turn
 * reply (`runId` set), since a routine or heartbeat post proves nothing.
 * Silent human readers are therefore not counted.
 */
import type { CanonicalRoomMessage, RoomMember } from "./types";

export type ReadState = { kind: "sent" } | { kind: "read" } | { kind: "read-by"; count: number };

type Seen = Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "runId">;

export function readState({
  message, messages, runs, members, viewerId,
}: {
  message: Seen;
  messages: readonly Seen[];
  runs: readonly { triggerMessageId?: string | null; agentId: string }[];
  members: readonly Pick<RoomMember, "kind" | "id">[];
  viewerId: string;
}): ReadState {
  const recipients = members.filter((m) => !(m.kind === "human" && m.id === viewerId));
  const isRecipient = (kind: string, id: string) => recipients.some((m) => m.kind === kind && m.id === id);
  const readers = new Set<string>();
  for (const run of runs) {
    if (run.triggerMessageId === message.messageId && isRecipient("agent", run.agentId)) readers.add(`agent:${run.agentId}`);
  }
  for (const later of messages) {
    if (later.messageSeq <= message.messageSeq) continue;
    const { kind, id } = later.author;
    if (kind === "system" || !isRecipient(kind, id)) continue;
    if (kind === "agent" && later.runId === null) continue;
    readers.add(`${kind}:${id}`);
  }
  if (readers.size === 0) return { kind: "sent" };
  return recipients.length === 1 ? { kind: "read" } : { kind: "read-by", count: readers.size };
}

export const readStateLabel = (state: ReadState): string =>
  state.kind === "sent" ? "Sent" : state.kind === "read" ? "Read" : `Read by ${state.count}`;

/** The viewer's newest live, canonical (seq > 0) message: the one that carries the read state. */
export function lastOwnMessageId(
  messages: readonly Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "deletedAt">[],
  viewerId: string,
): string | null {
  let best: { id: string; seq: number } | null = null;
  for (const m of messages) {
    if (m.author.kind !== "human" || m.author.id !== viewerId || m.deletedAt || m.messageSeq <= 0) continue;
    if (!best || m.messageSeq > best.seq) best = { id: m.messageId, seq: m.messageSeq };
  }
  return best?.id ?? null;
}

/**
 * An agent message the agent sent on its own. The backend stamps `runId` only
 * on a room-turn reply (`post_agent_message`), and exposes no finer kind, so
 * nightly check-ins, routines and finished-session notes share one label.
 */
export const UNPROMPTED_LABEL = "on its own";

export const unpromptedLabel = (message: Pick<CanonicalRoomMessage, "author" | "runId" | "deletedAt">): string | null =>
  message.author.kind === "agent" && message.runId === null && !message.deletedAt ? UNPROMPTED_LABEL : null;
```

Add to `packages/shared/src/index.ts`:

```ts
export { lastOwnMessageId, readState, readStateLabel, UNPROMPTED_LABEL, unpromptedLabel } from "./messaging/readState";
export type { ReadState } from "./messaging/readState";
```

- [ ] **Step 4: Run tests**

Run: `cd packages/shared && pnpm test src/messaging/readState.test.ts && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src/messaging/readState.ts packages/shared/src/messaging/readState.test.ts packages/shared/src/index.ts
git commit -m "feat(shared): read state and unprompted label from existing room data

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Bubble primitives

**Files:**
- Create: `apps/mobile/src/components/conversation/bubbles.tsx`
- Test: `apps/mobile/src/components/conversation/__tests__/bubbles.test.tsx`

**Interfaces:**
- Consumes: `ChatPalette`, `fonts`, `spacing` (theme), `usePulse` (`components/chat/usePulse`), `SymbolView`.
- Produces:
  ```ts
  export const BUBBLE_RADIUS = 18;
  export const AVATAR_GUTTER = 36;   // 28pt avatar + 8pt gap: where a collapsed bubble starts
  export function Bubble(props: {
    mine: boolean; palette: ChatPalette; children: ReactNode;
    /** A message that @mentions the viewer: the stroke turns link-blue (web's row tint, as a border). */
    mentionsMe?: boolean;
    dimmed?: boolean;                 // pending / unsent rows: opacity 0.6
    onLongPress?: () => void;         // delayLongPress 300
    accessibilityLabel: string;
    testID?: string;
  }): JSX.Element
  export function BubbleMeta(props: { align: "start" | "end"; palette: ChatPalette; children: ReactNode; indent?: number }): JSX.Element
  export function MetaText(props: { palette: ChatPalette; children: string; tone?: "mute" | "alert" | "link"; onPress?: () => void; accessibilityLabel?: string }): JSX.Element
  export function Chip(props: {
    label: string; palette: ChatPalette;
    icon?: SFSymbol;                  // e.g. "wrench.and.screwdriver", "sparkles", "person.2"
    onPress?: () => void; expanded?: boolean; accessibilityLabel?: string; selected?: boolean;
  }): JSX.Element
  export function WorkingBubble(props: { palette: ChatPalette; caption?: string | null; accessibilityLabel: string; testID?: string }): JSX.Element
  ```
- Behaviour:
  - `Bubble`: a `Pressable` (only `onLongPress`, no `onPress`, so links and chips inside stay tappable) styled per Global Constraints; mine `alignSelf: "flex-end"`, others `alignSelf: "flex-start"`; `mentionsMe` → `borderColor: palette.link`. `accessibilityRole` left default; `accessibilityLabel` from props.
  - `BubbleMeta`: a wrapping row (`flexDirection: "row"`, `flexWrap: "wrap"`, `alignItems: "center"`, gap 6, `marginTop: 3`, `marginHorizontal: 6`), `alignSelf` start/end, `marginLeft: indent` when given.
  - `MetaText`: `fonts.sans` 12/16, colour `fgMute` / `alert` / `link` by `tone`; with `onPress` it is a button with `hitSlop={12}`.
  - `Chip`: pill `backgroundColor: palette.chip`, radius 10, padding 3×8, row: optional `SymbolView` (11pt, `fgSoft`, hidden from VoiceOver), label (`fonts.sansMedium` 12/16 `fgSoft`), and when `onPress` a `chevron.right` (collapsed) / `chevron.down` (expanded) 9pt symbol. `hitSlop={10}`, `accessibilityRole="button"`, `accessibilityState={{ expanded }}` only when `onPress` and `expanded !== undefined`; `selected` → 1px `palette.fg` border (reaction the viewer applied).
  - `WorkingBubble`: an others-shaped bubble, row of three 6pt dots (`palette.fgMute`, each `Animated.View` with `opacity: usePulse(true, 0.3, 500, i * 160)`) then the caption (`fonts.sans` 14/19 `fgSoft`) when given; `accessibilityLabel` from props, `accessibilityLiveRegion="polite"`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/conversation/__tests__/bubbles.test.tsx
import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet, Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { Bubble, BubbleMeta, Chip, MetaText, WorkingBubble } from "../bubbles";

const p = chatPalette.light;

it("fills mine with ink on the right at 78%", async () => {
  await render(<Bubble mine palette={p} accessibilityLabel="You: hi" testID="b"><Text>hi</Text></Bubble>);
  expect(StyleSheet.flatten(screen.getByTestId("b").props.style)).toMatchObject({
    backgroundColor: p.fg, alignSelf: "flex-end", maxWidth: "78%", borderRadius: 18,
  });
});

it("draws others as a stroked pane bubble on the left at 94%", async () => {
  await render(<Bubble mine={false} palette={p} accessibilityLabel="CEO: hi" testID="b"><Text>hi</Text></Bubble>);
  expect(StyleSheet.flatten(screen.getByTestId("b").props.style)).toMatchObject({
    backgroundColor: p.surface, borderColor: p.edge, borderWidth: 1, alignSelf: "flex-start", maxWidth: "94%",
  });
});

it("strokes a bubble that mentions me in link blue", async () => {
  await render(<Bubble mine={false} mentionsMe palette={p} accessibilityLabel="x" testID="b"><Text>x</Text></Bubble>);
  expect(StyleSheet.flatten(screen.getByTestId("b").props.style).borderColor).toBe(p.link);
});

it("hands a long press to the caller", async () => {
  const onLongPress = jest.fn();
  await render(<Bubble mine={false} palette={p} onLongPress={onLongPress} accessibilityLabel="CEO: hi"><Text>hi</Text></Bubble>);
  await fireEvent(screen.getByLabelText("CEO: hi"), "longPress");
  expect(onLongPress).toHaveBeenCalled();
});

it("makes an expandable chip a button that reports its state", async () => {
  const onPress = jest.fn();
  await render(<Chip label="Ran 5 tools" palette={p} onPress={onPress} expanded={false} />);
  const chip = screen.getByLabelText("Ran 5 tools");
  expect(chip.props.accessibilityState).toMatchObject({ expanded: false });
  await fireEvent.press(chip);
  expect(onPress).toHaveBeenCalled();
});

it("hangs meta text under a bubble", async () => {
  await render(<BubbleMeta align="end" palette={p}><MetaText palette={p}>Read</MetaText></BubbleMeta>);
  expect(StyleSheet.flatten(screen.getByText("Read").props.style)).toMatchObject({ fontSize: 12, color: p.fgMute });
});

it("shows a working bubble with its caption, announced politely", async () => {
  await render(<WorkingBubble palette={p} caption="Browsing example.com" accessibilityLabel="CEO is working" />);
  expect(screen.getByText("Browsing example.com")).toBeTruthy();
  expect(screen.getByLabelText("CEO is working").props.accessibilityLiveRegion).toBe("polite");
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/components/conversation/__tests__/bubbles.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `bubbles.tsx`** to the Interfaces block.

- [ ] **Step 4: Run test**

Run: `cd apps/mobile && pnpm jest src/components/conversation/__tests__/bubbles.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/conversation
git commit -m "feat(mobile): bubble, chip, meta and working-bubble primitives

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: ConversationHeader

**Files:**
- Create: `apps/mobile/src/components/conversation/ConversationHeader.tsx`
- Test: `apps/mobile/src/components/conversation/__tests__/ConversationHeader.test.tsx`

**Interfaces:**
- Consumes: `ChatPalette`, `type` (`navTitle`, `navMeta`).
- Produces:
  ```ts
  export function ConversationHeader(props: {
    title: string;
    status?: string | null;          // "working…", "idle", "12 members", "on Leo’s Mac · idle"
    statusTone?: "mute" | "live";    // live = palette.live (an agent working / a busy session)
    avatar: ReactNode;               // 28pt Avatar (or the channel `#` tile)
    palette: ChatPalette;
    onPress?: () => void;            // opens details
  }): JSX.Element
  ```
- Behaviour: used as a native Stack `headerTitle`. A `Pressable` row (`accessibilityRole="button"`, `accessibilityLabel={status ? `${title}, ${status}` : title}`, `accessibilityHint="Shows details"`, disabled without `onPress`): avatar, then a column with the title (`type.navTitle`, colour `palette.fg`, 1 line, `maxFontSizeMultiplier={1}`) and the status (`type.navMeta` at 12pt, `palette.fgMute` or `palette.live`, 1 line, `maxFontSizeMultiplier={1}`). Max width `flexShrink: 1` so a long name truncates before the header buttons.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/conversation/__tests__/ConversationHeader.test.tsx
import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet, Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { ConversationHeader } from "../ConversationHeader";

const p = chatPalette.light;

it("names the conversation and its status, and opens details", async () => {
  const onPress = jest.fn();
  await render(<ConversationHeader title="CEO" status="working…" statusTone="live" avatar={<Text>A</Text>} palette={p} onPress={onPress} />);
  await fireEvent.press(screen.getByLabelText("CEO, working…"));
  expect(onPress).toHaveBeenCalled();
  expect(StyleSheet.flatten(screen.getByText("working…").props.style).color).toBe(p.live);
});

it("omits an empty status", async () => {
  await render(<ConversationHeader title="Leo" avatar={null} palette={p} />);
  expect(screen.getByLabelText("Leo")).toBeTruthy();
});
```

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/conversation/__tests__/ConversationHeader.test.tsx` → FAIL (module not found).

- [ ] **Step 3: Implement** to the Interfaces block.

- [ ] **Step 4: Run test** — same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/conversation/ConversationHeader.tsx apps/mobile/src/components/conversation/__tests__/ConversationHeader.test.tsx
git commit -m "feat(mobile): conversation header with avatar and status line

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: MessageGroup renders a message as a bubble

**Files:**
- Modify: `apps/mobile/src/components/room/MessageGroup.tsx`
- Test: `apps/mobile/src/components/room/__tests__/MessageGroup.test.tsx` (extend; update the mention-tint test)

**Interfaces:**
- Consumes: `Bubble`, `BubbleMeta`, `MetaText`, `Chip`, `AVATAR_GUTTER` (Task 3); `MessageMarkdown` `palette`/`inverse` (Task 1); `unpromptedLabel` (Task 2); `Avatar` (Part 1); `useChatPalette`.
- Produces — `MessageGroup` keeps every existing prop and adds:
  ```ts
  mine?: boolean;               // author is the viewer (callers pass author.kind === "human" && author.id === userId)
  showTime?: boolean;           // default true; false when the next bubble continues this sender's run
  readLabel?: string | null;    // "Sent" | "Read" | "Read by 2" — only on the viewer's last message
  agentRing?: AvatarRing | null;// ring on the run's first avatar (the agent is running now)
  ```
  `LocalMessageRows` keeps its props; its rows render as mine bubbles with `dimmed` and meta "Sending…" (in flight) or the existing "Not sent · tap to retry" as an `alert` `MetaText` button labelled "Not sent, tap to retry".
- Behaviour:
  - Layout: others — when not `collapsed`: a row with `Avatar size={28}` (`palette`, `ring={agentRing}`) and, above the bubble, the author name as `MetaText`-styled 12pt `fgMute` text with `numberOfLines={1}` (kept single-line: `conversation.test` counts author labels by `numberOfLines === 1`); collapsed → no avatar/name, bubble indented `AVATAR_GUTTER`. Mine — no avatar, no name, right-aligned. Top margin 12 for a new run, 2 for a collapsed bubble.
  - Bubble body: `MessageMarkdown` (`palette`, `inverse={mine}`) of `linkifyMentions(...)` as today; "Message deleted" (italic `fgMute` / `onFg` 60%) for deleted; `DocumentCards` and `LinkPreviews` inside the bubble under the text; image attachments inside the bubble (`width: "100%"`, `aspectRatio: 1`, radius 12, max 240pt) and file rows as `Chip`s with the `doc` / `play.rectangle` icon inside the bubble. Accessibility label `mine ? `You: ${text}` : `${author}: ${text}`` (deleted → "Message deleted").
  - Meta under the bubble (`BubbleMeta align={mine ? "end" : "start"} indent={mine ? 0 : AVATAR_GUTTER}`), in this order: reaction chips (`Chip label={`${emoji} ${count}`}` `selected` when the viewer reacted, `onPress` toggles, `accessibilityLabel` exactly today's "Add 👍 reaction, 2" / "Remove 👍 reaction, 2"); a replies chip `Chip label={`${n} ${n === 1 ? "reply" : "replies"}`}` opening the thread; the unprompted label `MetaText` (`unpromptedLabel(message)`); the time `MetaText` when `showTime` or after a long press (today's `timeShown`); `readLabel` `MetaText` when given. The reaction failure copy stays ("Could not update reaction. Try again.", `alert`, `accessibilityRole="alert"`).
  - `mentionsMe` → `Bubble mentionsMe`. The long press keeps `hapticImpact()` + `onLongPress` exactly as today.

- [ ] **Step 1: Write the failing tests** — in `MessageGroup.test.tsx` replace the whole-row tint test ("tints the whole row when the message mentions the viewer") with the bubble version and append:

```tsx
import { StyleSheet } from "react-native";
import { chatPalette } from "../../../config/theme";

const p = chatPalette.light; // jest's useColorScheme is "light"

it("strokes the bubble when the message mentions the viewer", async () => {
  await render(
    <MessageGroup message={{ ...message, mentions: [{ kind: "human", id: "user-a" }] }} collapsed={false} replyCount={0}
      currentUserId="user-a" downloadAttachment={downloadAttachment} attachmentUrl={attachmentUrl} />,
  );
  expect(StyleSheet.flatten(screen.getByTestId("message-bubble").props.style).borderColor).toBe(p.link);
});

describe("MessageGroup bubbles", () => {
  const base = { collapsed: false, replyCount: 0, currentUserId: "user-a", downloadAttachment, attachmentUrl };

  it("draws the viewer's message as a mine bubble with its read state", async () => {
    const own = { ...message, author: { kind: "human" as const, id: "user-a", displayName: "Ann" } };
    await render(<MessageGroup {...base} message={own} mine readLabel="Read" />);
    expect(StyleSheet.flatten(screen.getByTestId("message-bubble").props.style).backgroundColor).toBe(p.fg);
    expect(screen.getByLabelText("You: ship it")).toBeTruthy();
    expect(screen.getByText("Read")).toBeTruthy();
    expect(screen.queryByText("Ann")).toBeNull();
  });

  it("labels an agent message sent on its own", async () => {
    await render(<MessageGroup {...base} message={{ ...message, runId: null }} />);
    expect(screen.getByText("on its own")).toBeTruthy();
  });

  it("does not label a room-turn reply", async () => {
    await render(<MessageGroup {...base} message={{ ...message, runId: "run-1" }} />);
    expect(screen.queryByText("on its own")).toBeNull();
  });

  it("drops avatar and name on a continued run and hides the time until the run ends", async () => {
    await render(<MessageGroup {...base} message={message} collapsed showTime={false} />);
    expect(screen.queryByText("Friday")).toBeNull();
    expect(screen.queryByText(/\d:\d\d/)).toBeNull();
  });

  it("opens the thread from the replies chip", async () => {
    const onOpenThread = jest.fn();
    await render(<MessageGroup {...base} message={message} replyCount={3} onOpenThread={onOpenThread} />);
    await fireEvent.press(screen.getByLabelText("3 replies"));
    expect(onOpenThread).toHaveBeenCalled();
  });

  it("never carries a read state on a deleted message", async () => {
    await render(<MessageGroup {...base} message={{ ...message, deletedAt: "2026-09-26T00:00:00Z" }} mine readLabel="Read" />);
    expect(screen.getByText("Message deleted")).toBeTruthy();
    expect(screen.queryByText("Read")).toBeNull();
  });
});

describe("LocalMessageRows", () => {
  it("dims an in-flight row as Sending… and offers retry on a failed one", async () => {
    const onRetry = jest.fn();
    const own = { ...message, messageId: "local-1", messageSeq: 0, author: { kind: "human" as const, id: "user-a", displayName: "Ann" } };
    await render(
      <LocalMessageRows rows={[{ message: own, unsent: false }, { message: { ...own, messageId: "local-2" }, unsent: true }]}
        onRetry={onRetry} downloadAttachment={downloadAttachment} attachmentUrl={attachmentUrl} currentUserId="user-a" />,
    );
    expect(screen.getByText("Sending…")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Not sent, tap to retry"));
    expect(onRetry).toHaveBeenCalledWith("local-2");
  });
});
```

(Import `LocalMessageRows` alongside `MessageGroup`. The existing reaction/attachment/viewer tests keep their assertions: labels, texts and callbacks are unchanged.)

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/room/__tests__/MessageGroup.test.tsx` → FAIL (no `message-bubble`, no read state, no label).

- [ ] **Step 3: Implement** to the Interfaces/Behaviour blocks (`Bubble testID="message-bubble"`; read `const palette = useChatPalette()` inside the component).

- [ ] **Step 4: Run tests** — `cd apps/mobile && pnpm jest src/components/room/__tests__/MessageGroup.test.tsx && pnpm typecheck` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/room/MessageGroup.tsx apps/mobile/src/components/room/__tests__/MessageGroup.test.tsx
git commit -m "feat(mobile): room messages as bubbles with hanging meta and read state

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: RunBlock as a working / streaming bubble

**Files:**
- Modify: `apps/mobile/src/components/room/RunBlock.tsx`
- Test: `apps/mobile/src/components/room/__tests__/RunBlock.test.tsx` (extend)

**Interfaces:**
- Consumes: `WorkingBubble`, `Bubble`, `BubbleMeta`, `Chip`, `MetaText`, `AVATAR_GUTTER` (Task 3); `MessageMarkdown` (Task 1); `ToolPill` (existing, unchanged); `browsingSite`, `RoomRunView` (shared); `useChatPalette`.
- Produces: `RunBlock` keeps `{ run, name, onAbort?, onSteer? }` and adds `export function runCaption(run: RoomRunView, name: string): string`:
  - browsing → `site ? `Browsing ${site}` : "Browsing"`;
  - else the newest tool with `status === "running"` → `Using ${tool.name}`;
  - else `${name} is working`.
- Behaviour (replaces `AgentHead` + bare text):
  - Running with no text → `WorkingBubble caption={runCaption(run, name)} accessibilityLabel={`${name}: ${runCaption(run, name)}`}` indented `AVATAR_GUTTER`, preceded by the agent's 28pt avatar + name label (as the first bubble of a run).
  - Text present → an others `Bubble` with `MessageMarkdown` of `run.text` (streaming), then meta.
  - Meta (`BubbleMeta`, start, indented): a tools chip when `run.tools.length > 0` — label `${running ? "Running" : "Ran"} ${n} ${n === 1 ? "tool" : "tools"}`, `icon="wrench.and.screwdriver"`, toggles an inline list of today's `ToolPill`s under the meta; the browsing line (today's `Linking.openURL(browserPanelUrl())` link, now a `Chip icon="globe"` labelled `Watch ${site ?? "browser"}` with the existing accessibility label); an outcome `MetaText` for a run that ended without a reply: `failed` → "Didn't finish" (`alert`), `aborted` → `forcedStop ? "Stopped — may finish its current step" : "Stopped"`.
  - Steer/Stop controls stay under a running run, restyled: the steer field becomes a pill (`surface`, 1px `edge`, radius 18, 15pt) with the 28pt `fg`/`onFg` send circle; Stop is an `fg` pill with `onFg` label. Every label, handler, latch and error copy is unchanged ("Steer this run…", `Stop ${name}`, `Stopping ${name}`, `STEER_FAILED`, `ABORT_FAILED`).

- [ ] **Step 1: Write the failing tests** (append; reuse the file's existing `run(...)` fixture helper and mocks):

```tsx
import { runCaption } from "../RunBlock";

describe("RunBlock bubbles", () => {
  it("shows a working bubble with the run's caption before any text", async () => {
    await render(<RunBlock run={{ ...runningRun, text: "", tools: [] }} name="CEO" />);
    expect(screen.getByLabelText("CEO: CEO is working")).toBeTruthy();
  });

  it("captions the tool in use, and a browsing run by its site", () => {
    const tool = { id: "t", name: "Bash", args: {}, result: null, status: "running" as const };
    expect(runCaption({ ...runningRun, tools: [tool] }, "CEO")).toBe("Using Bash");
    const nav = { id: "n", name: "browser_navigate", args: { url: "https://www.example.com/x" }, result: null, status: "done" as const };
    expect(runCaption({ ...runningRun, tools: [nav] }, "CEO")).toBe("Browsing example.com");
  });

  it("folds tools into one chip that expands to the pills", async () => {
    const done = { id: "t", name: "Bash", args: {}, result: null, status: "done" as const };
    await render(<RunBlock run={{ ...runningRun, text: "Rolling the fleet", tools: [done, { ...done, id: "u" }] }} name="CEO" />);
    expect(screen.getByText("Rolling the fleet")).toBeTruthy();
    const chip = screen.getByLabelText("Running 2 tools");
    await fireEvent.press(chip);
    expect(screen.getAllByText(/Bash/).length).toBeGreaterThan(0);
  });

  it("says a stopped run stopped", async () => {
    await render(<RunBlock run={{ ...runningRun, state: "aborted", forcedStop: true, text: "" }} name="CEO" />);
    expect(screen.getByText("Stopped — may finish its current step")).toBeTruthy();
  });
});
```

If `RunBlock.test.tsx` has no shared `runningRun` fixture, declare one at the top of this `describe`:

```tsx
const runningRun: RoomRunView = {
  roomId: "room-1", agentId: "ceo", runId: "run-1", triggerMessageId: "m1", threadRootMessageId: null,
  afterSeq: 1, text: "", thinking: "", state: "running", tools: [],
};
```

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/room/__tests__/RunBlock.test.tsx` → FAIL.

- [ ] **Step 3: Implement** to the Interfaces/Behaviour blocks.

- [ ] **Step 4: Run tests** — same command (all existing Stop/steer/browsing tests included) → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/room/RunBlock.tsx apps/mobile/src/components/room/__tests__/RunBlock.test.tsx
git commit -m "feat(mobile): live runs as working and streaming bubbles with a tools chip

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: RoomComposer — "+" · pill field · send

**Files:**
- Modify: `apps/mobile/src/components/room/RoomComposer.tsx` (render tree from `<View style={styles.card}>` to the end of the rail; `styles`)
- Test: `apps/mobile/src/components/room/__tests__/RoomComposer.test.tsx` (extend)

**Interfaces:**
- Consumes: `useChatPalette`.
- Produces: no prop changes. Every accessibility label is kept ("Attach photo", "Insert emoji", "Formatting", "Send", "Stop"), so the existing suite keeps passing.
- Layout (top to bottom inside the existing outer `Reanimated.View`, which becomes `backgroundColor: palette.ground` with a hairline `palette.edge` top border):
  1. error/limit line (unchanged copy, `palette.alert`);
  2. `AttachmentChips` (unchanged);
  3. the "who will answer" hint as its own line when non-empty (`fonts.sans` 12/16 `palette.fgMute`, left, `numberOfLines={1}`, `maxFontSizeMultiplier={1.4}`) — it leaves the rail;
  4. the format row when `formatOpen` (unchanged controls, icons tinted `palette.fgMute`);
  5. the input row, `flexDirection: "row"`, `alignItems: "flex-end"`, gap 8:
     - **"+"** — 32pt circle, `palette.chip` fill, `plus` symbol 16pt `palette.fgSoft`; it IS today's attach button (same `attach` handler, label "Attach photo", same disabled rule, same uploading spinner);
     - **pill** (flex 1) — `palette.surface`, 1px `palette.edge`, radius 18, `paddingLeft: 12`, `paddingRight: 4`, a row of the `TextInput` (16/22 `fonts.sans`, `palette.fg`, placeholder `palette.fgMute`, same min/max heights and handlers) and, bottom-aligned inside the pill, the two 28pt glyph buttons `textformat` ("Formatting", keeps `accessibilityState.expanded`) and `face.smiling` ("Insert emoji"), tinted `palette.fgMute` (`palette.fg` when formatting is open);
     - **send / stop** — 32pt circle, `palette.fg` fill, `arrow.up` / `stop.fill` in `palette.onFg`; disabled opacity 0.4; same press-scale spring and same Stop-takes-Send's-slot rule.
  6. The reconnecting banner keeps its copy; colours → `palette.raised` fill, `palette.warn` border/text/dot.

- [ ] **Step 1: Write the failing test** (append):

```tsx
import { StyleSheet } from "react-native";
import { chatPalette } from "../../../config/theme";

it("draws the send button in ink and the field as a pill", async () => {
  await render(<RoomComposer {...defaultProps} />);
  await fireEvent.changeText(screen.getByPlaceholderText(defaultProps.placeholder), "hello");
  expect(StyleSheet.flatten(screen.getByLabelText("Send").props.style).backgroundColor).toBe(chatPalette.light.fg);
  expect(StyleSheet.flatten(screen.getByTestId("composer-pill").props.style)).toMatchObject({
    borderRadius: 18, backgroundColor: chatPalette.light.surface, borderColor: chatPalette.light.edge,
  });
});

it("puts the attach action on the leading plus", async () => {
  await render(<RoomComposer {...defaultProps} />);
  expect(screen.getByTestId("composer-plus")).toBeTruthy();
  expect(screen.getByLabelText("Attach photo")).toBeTruthy();
});
```

(`defaultProps` is the fixture the file already renders with; if it is inlined per test, extract it to a `const defaultProps = { … }` at the top first, unchanged. The plus `Pressable` carries `testID="composer-plus"`, the pill `View` `testID="composer-pill"`.)

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/room/__tests__/RoomComposer.test.tsx` → FAIL.

- [ ] **Step 3: Implement** the layout above; delete the now-unused `rail`, `railButton`, `railButtonActive`, `card`, `inputZone` styles.

- [ ] **Step 4: Run tests** — same command → PASS (all 30-odd existing composer tests included).

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/room/RoomComposer.tsx apps/mobile/src/components/room/__tests__/RoomComposer.test.tsx
git commit -m "feat(mobile): composer as plus, pill field and send

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: The room screen on bubbles

**Files:**
- Modify: `apps/mobile/src/app/(app)/room/[roomId]/index.tsx`
- Modify: `apps/mobile/src/components/room/RoomHeaderActions.tsx` (tint `palette.fgSoft`)
- Test: `apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx`

**Interfaces:**
- Consumes: Tasks 2–7; `otherMembers` (Part 1 `lib/chats`); `agentRingState` is NOT used here (the room's own `rooms.runs` is the live truth).
- Behaviour:
  - Background `palette.ground` (container + `contentStyle` via `Stack.Screen options`).
  - Header: `headerTitle: () => <ConversationHeader title avatar status statusTone palette onPress={() => router.push(`/(app)/room/${roomId}/details`)} />`, where:
    - avatar: channel → the 28pt `#` tile (`palette.selected`, radius 8, `#` `fonts.sansSemiBold` 14 `palette.fgSoft`); dm → the counterpart's `Avatar size={28}` with `ring="running"` while that agent has a running run, `showPresence`; group-dm → the first other member's avatar;
    - status: agent DM → `"working…"` (`live`) while `runs.some((r) => r.state === "running" && r.agentId === others[0].id)`, else `"idle"`; channel → `${n} member(s)` plus `" · Shared with Lightbulb"` when `room.shared`; group-dm → `${members.length} people`; human DM → none (no presence data on mobile);
    - `headerRight` keeps `RoomHeaderActions` (star/mute) unchanged in behaviour.
  - Timeline items (still the inverted `FlatList`, `newestFirst`): date divider → centred `MetaText`-styled 12pt `fgMute` label, no rules; "New messages" → centred 12pt `fonts.sansSemiBold` `palette.link` between two hairline `palette.link` rules.
  - For each message item pass to `MessageGroup`: `mine = item.message.author.kind === "human" && item.message.author.id === userId`; `showTime` = the NEXT item in `items` (oldest→newest) is not a collapsed message of the same author (compute a `Set` of message ids whose successor collapses into them, once per `items` change); `readLabel = item.message.messageId === lastOwnId ? readStateLabel(readState({ message, messages: mainMessages, runs: rooms.runs, members, viewerId: userId })) : null` where `lastOwnId = lastOwnMessageId(mainMessages, userId)`; `agentRing = "running"` when that agent has a running run and the item is the newest message of its run.
  - Typing: replace the `typingText` `<Text>` above the composer with, as the first element of `ListHeaderComponent` (the bottom of the inverted list, under the newest content), `WorkingBubble accessibilityLabel={typingText}` with no caption, indented `AVATAR_GUTTER`, rendered only while `typingText` is non-empty.
  - `LocalMessageRows`, `RunBlock` placement (`placed.tail` / `placed.after`), `CatchUpSummary`, `OnboardingBand`, blocked states, jump-to-latest (now an `fg` circle with an `onFg` arrow), actions sheet, reaction errors, typing report, drafts, peek behaviour, `Load older messages` button: all unchanged in behaviour.

- [ ] **Step 1: Update and add tests** in `conversation.test.tsx`:
  - "shows the author name once per run…" — keep as is (author label is still single-line and once per run).
  - "shows another member's typing line with the web's exact wording": change the final assertion to `await waitFor(() => expect(screen.getByLabelText("Bob is typing…")).toBeTruthy());` and the initial one to `expect(screen.queryByLabelText(/typing…/)).toBeNull();`.
  - Append:

```tsx
it("titles an agent DM with the agent and its idle status, opening details", async () => {
  mockRoomId = "dm-friday";
  // The suite's getRoom mock returns this detail for "dm-friday": kind "dm", members [Alice (viewer), Friday (agent)].
  await render(<Conversation />);
  await fireEvent.press(await screen.findByLabelText("Friday, idle"));
  expect(mockPush).toHaveBeenCalledWith("/(app)/room/dm-friday/details");
});

it("shows Sent under my last message until someone answers, then Read by", async () => {
  await render(<Conversation />);
  // Fixture: channel "room-1" whose newest message (seq 3) is Alice's (the viewer), nothing after it.
  await waitFor(() => expect(screen.getByText("Sent")).toBeTruthy());
  await act(async () => {
    for (const handler of typingHandlers) {
      handler({ schemaVersion: 1, eventId: "e-9", type: "room.message.created", occurredAt: "2026-06-11T10:05:00Z", roomId: "room-1", messageSeq: 4, roomRevision: 1, payload: agentReply } as LiveEvent);
    }
  });
  await waitFor(() => expect(screen.getByText("Read by 1")).toBeTruthy());
});
```

  For these two, extend the suite's fixtures: add a `"dm-friday"` `RoomDetail` to the `getRoom` mock map (kind `"dm"`, members Alice human `user-a` + Friday agent `friday`, `lastMessageSeq: 0`), make the `"room-1"` fixture's newest message (seq 3) authored by `user-a`, and declare `const agentReply: CanonicalRoomMessage = { messageId: "m-agent", roomId: "room-1", messageSeq: 4, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "on it", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-06-11T10:05:00Z", runId: "run-9" };`. The handler set is the one the suite already registers through `onEvent` (it is named `typingHandlers` but receives every event); a `room.message.created` envelope's `payload` IS the `CanonicalRoomMessage` (`live/types.ts:222`).

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest 'src/app/\(app\)/room/__tests__/conversation.test.tsx'` → FAIL on the new/changed tests.

- [ ] **Step 3: Implement** the Behaviour list.

- [ ] **Step 4: Run tests** — same command → PASS (every pre-existing room test included); `pnpm typecheck`.

- [ ] **Step 5: Commit**

```bash
git add 'apps/mobile/src/app/(app)/room/[roomId]/index.tsx' apps/mobile/src/components/room/RoomHeaderActions.tsx 'apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx'
git commit -m "feat(mobile): room screen as a bubble conversation with read state

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: The thread screen on bubbles

**Files:**
- Modify: `apps/mobile/src/app/(app)/room/[roomId]/thread/[messageId].tsx`
- Test: `apps/mobile/src/app/(app)/room/__tests__/thread.test.tsx`

**Interfaces:**
- Consumes: Tasks 2–7.
- Behaviour: background `palette.ground`; the thread root renders as a `MessageGroup` with `collapsed={false}` and `showTime`; replies as `MessageGroup`s with `mine`, `showTime` and `readLabel` computed exactly as Task 8 but over the thread's own messages (`threadRootMessageId === root`) and runs filtered to this thread; the typing line becomes the same `WorkingBubble` (label = `typingText`) at the bottom of the replies; header title stays "Thread" with the room name as `headerTitle` status via `ConversationHeader` (`title="Thread"`, `status={roomTitle}`, avatar `null`, no `onPress`). Composer, runs, local rows, actions sheet unchanged in behaviour.

- [ ] **Step 1: Add the failing test** to `thread.test.tsx`:

```tsx
it("draws the viewer's reply as a mine bubble with Sent under it", async () => {
  await render(<Thread />);
  // Fixture: the thread's newest reply is the viewer's (user-a), nothing after it.
  await waitFor(() => expect(screen.getByText("Sent")).toBeTruthy());
  const bubbles = screen.getAllByTestId("message-bubble");
  expect(bubbles.length).toBeGreaterThan(1);
});
```

(Adjust the suite's reply fixture so its newest reply is authored by the viewer, `user-a`, if it is not already. If the suite's typing assertions read `getByText(/typing…/)`, switch them to `getByLabelText` as in Task 8.)

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest 'src/app/\(app\)/room/__tests__/thread.test.tsx'` → FAIL.

- [ ] **Step 3: Implement** the Behaviour block.

- [ ] **Step 4: Run tests** — same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add 'apps/mobile/src/app/(app)/room/[roomId]/thread/[messageId].tsx' 'apps/mobile/src/app/(app)/room/__tests__/thread.test.tsx'
git commit -m "feat(mobile): thread screen on bubbles

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Session transcript and follow-up composer in the bubble language

**Files:**
- Modify: `apps/mobile/src/components/session/TranscriptRow.tsx`
- Modify: `apps/mobile/src/components/session/SessionPromptRow.tsx`
- Test: `apps/mobile/src/components/session/__tests__/TranscriptRow.test.tsx` (rewrite the glyph-grammar assertions)

**Interfaces:**
- Consumes: `Bubble`, `BubbleMeta`, `MetaText`, `Chip`, `WorkingBubble` (Task 3); `MessageMarkdown` (Task 1); shared `toolHeader`, `toolRowView`, `thinkingLabel`, `subagentLabel`, `subagentStatus`, `subagentTags`, `lifecycleLabel`, `buildRows`, `groupRows`, `PREVIEW_LINES`; `useChatPalette`.
- Produces (exports unchanged in name; new optional props):
  ```ts
  TranscriptRowView(props: { provider; row; name: (who: string | null) => string; live?; events?; status?;
    /** True when this prompt was written by the viewer (owner viewing: who === null; teammate: who === viewerId). */
    isMine?: (who: string | null) => boolean;   // default: who === null
  })
  PendingPromptRow(props: { provider; text; ownerName; chip: SessionReceiptChip | null })   // mine bubble, dimmed, chip label as read state
  WorkingRow(props: { provider; since?: string })                                           // WorkingBubble "Working · 12s", label "Working"
  ReceiptChipText(props: { chip })                                                          // unchanged API, MetaText-styled
  SessionPromptRow(props unchanged)
  ```
- Row mapping (no `GLYPH` anywhere; `import { GLYPH }` is removed from both files):
  - `prompt` → `Bubble mine={isMine(row.who)}` with the text as `MessageMarkdown` (`inverse` when mine); others' prompts get the author name above (12pt `fgMute`); meta = time (`HH:MM`).
  - `prose` → others `Bubble` with `MessageMarkdown`; Codex `final` rows get meta `MetaText` "final answer" (replaces the mono "codex" label).
  - `thinking` → `Chip icon="sparkles"` labelled `thinkingLabel(row, live)` ("Thought for 5s" / "Thinking…"); expandable when `row.text` → the thinking markdown in a `palette.raised` well (radius 12, padding 10, `fgSoft`).
  - `tools` group → `Chip icon="wrench.and.screwdriver"` labelled `row.summary` ("Ran 2 tools · 1 edit"), expanding to the individual tool rows.
  - `tool` → a `Chip` labelled `toolHeader(provider, row.call)` (Source Sans — e.g. "Bash(git status)"), plus " · running…" while running and an `alert` "Error" `MetaText` on error; expanding (or always, when there is a diff) shows the output well: `fonts.mono` 12/17 on `palette.raised`, diff lines coloured `live` (+) / `alert` (−) / `fgSoft`, and the existing "… +N lines" / "show output" / "collapse" toggle as `link` `MetaText` buttons (same words, same `accessibilityState.expanded`).
  - `agent` (sub-agent) → `Chip icon="person.2"` labelled `[label, status].join(" — ")` expanding to the nested rows (unchanged nesting logic).
  - `notice`, `subagent`, `lifecycle` → centred 12pt `fgMute` text (e.g. "Worked for 12s").
- `SessionPromptRow`: the glyph goes; the input becomes the same pill as Task 7 (`palette.surface`, 1px `edge`, radius 18, `fonts.sans` 16/22 — no mono), placeholder `Send a follow-up to ${target}…` (label unchanged "Send a follow-up to Claude"), send = 32pt `fg` circle with `onFg` arrow (label "Send follow-up"); the reason line (`warn`), error (`alert`), Check delivery / Retry same request (`link`) are unchanged in wording and behaviour; bar background `palette.ground` with a hairline `edge` top border.

- [ ] **Step 1: Rewrite the failing tests** in `TranscriptRow.test.tsx`:

```tsx
it("draws a teammate's prompt as a left bubble with attribution, prose and a thought chip", async () => {
  await draw("claude", [
    ev(1, { kind: "text", role: "user", text: "run it", actor: { agentId: "friday" }, ts: "2026-09-12T10:00:00.000Z" }),
    ev(2, { kind: "thinking", role: "assistant", text: "" }),
    ev(3, { kind: "text", role: "assistant", text: "All green.", itemId: "m1" }),
  ]);
  expect(screen.queryByText("❯")).toBeNull();
  expect(screen.getByText("run it")).toBeTruthy();
  expect(screen.getByText("Friday")).toBeTruthy();
  expect(screen.getByLabelText("Thought")).toBeTruthy();
  expect(screen.getByText("All green.")).toBeTruthy();
});

it("draws the viewer's own prompt as a mine bubble", async () => {
  await render(<TranscriptRowView provider="claude" row={buildRows([ev(1, { kind: "text", role: "user", text: "ship it" })])[0]} name={name} isMine={(who) => who === null} />);
  expect(screen.getByLabelText("You: ship it")).toBeTruthy();
});
```

Keep the other existing tests, changing only what the glyph grammar forced: "collapses thinking…" — unchanged labels ("Thought for 5s", "Ran 2 tools · 1 edit", "Bash(npm test)") still hold; "collapses tool output…" — press the `Bash(...)` chip first when the output is not shown by default, then the existing "… +N lines" text; "draws a diff…Codex final answer" — replace the `"codex"` label assertion with `expect(screen.getByText("final answer")).toBeTruthy()` and drop any assertion on `⎿`; "shows a working line with elapsed seconds" — `expect(screen.getByLabelText("Working")).toBeTruthy(); expect(screen.getByText(/Working · \d+s/)).toBeTruthy();`; "draws the unsent follow-up greyed with one status chip" — assert the chip label text (e.g. "Queued on your Mac") is present and `screen.queryByText("❯")` is null. Add one guard test:

```tsx
it("uses no monospace outside code", async () => {
  await draw("claude", [
    ev(1, { kind: "text", role: "user", text: "go" }),
    ev(2, { kind: "tool", role: "assistant", text: "ls", itemId: "t", tool: { name: "Bash", status: "done", input: { command: "ls" } } }),
  ]);
  const monos = screen.UNSAFE_root.findAll((n) => n.props?.style && StyleSheet.flatten(n.props.style)?.fontFamily === "Menlo");
  expect(monos).toHaveLength(0); // output well is collapsed; nothing else may be mono
});
```

(import `StyleSheet` from `react-native`.)

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/session/__tests__/TranscriptRow.test.tsx` → FAIL.

- [ ] **Step 3: Implement** the mapping; delete the glyph styles (`glyph`, `glyphSpace`, `thinkingGlyph`, `workingGlyph`, `codexLabel`, `promptBand`, `pendingBand`).

- [ ] **Step 4: Run tests** — same command → PASS; `pnpm typecheck`.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/session/TranscriptRow.tsx apps/mobile/src/components/session/SessionPromptRow.tsx apps/mobile/src/components/session/__tests__/TranscriptRow.test.tsx
git commit -m "feat(mobile): session transcript as bubbles and chips, no TUI glyphs

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: The session screen — header, details sheet, scroll-to-load

**Files:**
- Modify: `apps/mobile/src/app/(app)/session/[sessionId].tsx`
- Test: `apps/mobile/src/app/(app)/__tests__/session.test.tsx`

**Interfaces:**
- Consumes: `ConversationHeader` (Task 4), Task 10 components, `Avatar` (`vendor`, `ring`), `sessionRingState` (Part 1 shared), existing `BottomSheet`, `SessionOverview`.
- Behaviour:
  - Background `palette.ground`.
  - Header: `headerTitle: () => <ConversationHeader title={sessionLabel(session)} avatar={<Avatar name={sessionLabel(session)} kind="agent" vendor={session.provider} ring={sessionRingState(session)} size={28} palette />} status={`on ${own ? "your" : `${ownerName}’s`} Mac · ${sessionStatus[session.status].toLowerCase()}`} statusTone={session.status === "busy" ? "live" : "mute"} palette onPress={() => setDetailsOpen(true)} />`. `headerRight` (Message owner) unchanged.
  - The two grey summary lines and the pinned `SessionOverview` leave the body. A `BottomSheet large` details sheet (`visible={detailsOpen}`) holds, in order: `${ownerName} · ${sessionStatus[status]}${cwdName ? ` · ~/${cwdName}` : ""}` (15pt `fg`), the existing follow-ups/approvals sentence verbatim (13pt `fgSoft`), the `unavailableReason` line (`warn`) when present, the waiting block (same copy as today, including "Answer it on your Mac; approvals are not sent from here." and the Message owner link), then `<SessionOverview events={events} />`. The sheet is pinned light by `BottomSheet`; its content uses `chatPalette.light` explicitly.
  - While `session.status === "waiting-approval"`, a one-line banner stays in the body above the list (`palette.surface`, 1px `palette.alert` border, radius 12): `own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal``, with a trailing "Details" `link` button opening the sheet. `accessibilityLiveRegion="polite"`.
  - Older history: remove the "Load older" button; the `FlatList` gets `onStartReached={() => { if (history.hasOlder && !history.loading) void history.load("older"); }}`, `onStartReachedThreshold={0.2}`, `maintainVisibleContentPosition={{ minIndexForVisible: 0 }}`, and a `ListHeaderComponent` `ActivityIndicator` (`palette.fgMute`) while `history.loading && history.hasOlder`. The history error card and "Jump to latest" link stay.
  - `TranscriptRowView` gets `isMine={(who) => (who === null ? own : who === userId)}`; the jump-to-bottom button becomes an `fg` circle with an `onFg` arrow; read-only footer copy unchanged.

- [ ] **Step 1: Update the failing tests** in `session.test.tsx`:
  - Replace "titles the screen with the tool's logo…":

```tsx
it("titles the screen with the tool's mark, the session and where it runs", async () => {
  await render(<SessionScreen />);
  const header = await screen.findByLabelText("Claude · Fix relay, on your Mac · idle");
  expect(header.props.accessibilityRole).toBe("button");
  expect(screen.getByTestId("session-tool-claude", { includeHiddenElements: true })).toBeTruthy();
});
```

  - In "renders the transcript in the Claude grammar…": replace `expect(screen.getAllByText(/^Alice · /)).toHaveLength(2);` with `expect(screen.getByLabelText("You: run it")).toBeTruthy();`; replace `expect(screen.getByText("Alice · Idle · ~/lightbulb")).toBeTruthy();` with opening the sheet — `await fireEvent.press(screen.getByLabelText("Claude · Fix relay, on your Mac · idle")); expect(screen.getByText("Alice · Idle · ~/lightbulb")).toBeTruthy();`; before pressing `"… +2 lines"`, press the `Bash(git status)` chip.
  - "says 'your Mac' and 'your terminal'…": the banner text "Waiting on you in your terminal" is still in the body; the "your Mac" follow-ups sentence is asserted after opening the sheet.
  - Append:

```tsx
it("loads older history when the reader scrolls to the top", async () => {
  mockSession = { ...base, latestEventSeq: 120 };
  await render(<SessionScreen />);
  const list = await screen.findByTestId("session-transcript");
  await act(async () => { list.props.onStartReached?.({ distanceFromStart: 0 }); });
  await waitFor(() => expect(mockListEvents.mock.calls.length).toBeGreaterThan(1));
});
```

  (`FlatList` gets `testID="session-transcript"`; `mockListEvents` must report an older page for `latestEventSeq: 120` — the suite's mock returns `events` regardless, and `useLocalSessionHistory` marks `hasOlder` when the first page starts above seq 1, so set `mockEvents` to five events with seqs 116–120 in this test.)

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest 'src/app/\(app\)/__tests__/session.test.tsx'` → FAIL.

- [ ] **Step 3: Implement** the Behaviour list.

- [ ] **Step 4: Run tests** — same command → PASS; `pnpm typecheck && pnpm lint`.

- [ ] **Step 5: Commit**

```bash
git add 'apps/mobile/src/app/(app)/session/[sessionId].tsx' 'apps/mobile/src/app/(app)/__tests__/session.test.tsx'
git commit -m "feat(mobile): session screen header, details sheet and scroll-to-load

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Verification, fingerprint and simulator acceptance

- [ ] **Step 1: Targeted suites, typecheck, lint**

```bash
cd packages/shared && pnpm test src/messaging/readState.test.ts src/messaging/chatList.test.ts && pnpm typecheck
cd ../../apps/mobile && pnpm jest src/components/chat src/components/conversation src/components/room src/components/session 'src/app/\(app\)/room' 'src/app/\(app\)/__tests__/session.test.tsx' && pnpm typecheck && pnpm lint
```
Expected: PASS, zero lint errors.

- [ ] **Step 2: Fingerprint unchanged** — `git diff --stat origin/main -- apps/mobile/app.json apps/mobile/package.json pnpm-lock.yaml` prints nothing; then compare hashes exactly as Part 1 Task 12 Step 2 (branch `npx @expo/fingerprint .` vs a throwaway `git worktree add /tmp/fp-base origin/main`; never `git stash`). Expected: equal hashes.

- [ ] **Step 3: Simulator acceptance (manual, on the PR)** — `pnpm start:dev` + `pnpm ios`, a dev workspace with an agent DM, a group channel with two other members, and a shared Claude Code session. For light and dark (`xcrun simctl ui booted appearance light|dark`; `xcrun simctl io booted screenshot <name>.png`) capture:
  1. an agent DM with a long markdown reply (heading, list, link, inline code, a wide fenced block that scrolls inside its well), the "Ran N tools ›" chip expanded, and the working bubble with its caption while a run is live;
  2. an unprompted agent message showing "on its own";
  3. my last message reading "Sent", then "Read" after the agent answers;
  4. a group channel showing "Read by 2", a reaction chip, a replies chip, a mention-stroked bubble, and another member's typing bubble;
  5. a thread;
  6. a Claude Code session: header "Claude · … / on …'s Mac · working", my follow-up as a mine bubble with "Queued on your Mac"/"Delivered", tool/thought/sub-agent chips, the details sheet, the waiting banner if one is live;
  7. the composer with text, and the steer/stop controls under a live run.
  Check: no monospace outside code, no ❯ ⏺ ⎿ ✻ glyphs or boxed characters, bubbles cap at 78% / 94%, text legible in both appearances, VoiceOver reads each bubble as "Name: text". Attach all screenshots to the PR.

- [ ] **Step 4:** open the PR `feat(mobile): iPhone redesign part 2 — conversation`, body ending `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

---

## Self-Review

- **Spec coverage (Part 2):** one component family for room and session → Tasks 3–11; header with avatar/name/status and tap → details (RoomDetails route; session details sheet = today's summary + `SessionOverview`) → Tasks 4, 8, 11; read state Sent → Read / Read by N (Delivered only where it exists: sessions) → Tasks 2, 5, 8, 9, 10; composer "+" / growing field / send, session "Send a follow-up to Claude…" without mono → Tasks 7, 10; session chips for tools / sub-agents / thoughts, approvals copy moved into the sheet, scroll-to-load older → Tasks 10, 11; bubbles (78% mine / 94% others, 18pt radius, avatar+name on first of run) → Tasks 3, 5; hanging chips/reactions/replies/time/read state → Task 5; working bubble with caption, typing bubble with dots → Tasks 3, 6, 8; unprompted labels → Tasks 2, 5; markdown in bubbles, mono only in code → Tasks 1, 10; Reduce Motion → Task 3 (`usePulse`); preserved features (threads, reactions, actions sheet, attachments, link previews, document cards, run blocks, catch-up, onboarding band, blocked state) → Tasks 5, 6, 8, 9.
- **Spec gaps resolved:** "Delivered" for rooms and other members' `lastReadSeq` are not exposed → derived from runs and later replies, stated in Global Constraints; unprompted kinds are not distinguishable → one label; run captions have no elapsed time for room runs (`LiveRoomRun` carries no start time) → the caption names the activity only; persisted room messages carry no tool record (`visibleRuns` drops a run once its reply lands) → the tools chip exists while the run is on screen, and for sessions always; human "online" status → omitted.
- **Type consistency:** `ReadState`/`readStateLabel`/`lastOwnMessageId`/`unpromptedLabel` (Task 2) used in Tasks 5, 8, 9; `Bubble`/`BubbleMeta`/`MetaText`/`Chip`/`WorkingBubble`/`AVATAR_GUTTER` (Task 3) used in 5, 6, 8–11; `MessageGroup` new props `mine`/`showTime`/`readLabel`/`agentRing` used in 8–9; `TranscriptRowView` `isMine` used in 11.
