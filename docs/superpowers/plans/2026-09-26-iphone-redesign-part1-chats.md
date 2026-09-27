# iPhone Redesign Part 1 — Chats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Home · DMs · Activity · More with Chats · Activity · Teams · Settings, where Chats is one merged, recency-sorted list of rooms and live Claude Code / Codex sessions under a pinned strip, on a new light+dark visual foundation.

**Architecture:** A `chatPalette` (light + dark, the web `[data-chat-theme]` values) and `useChatPalette()` join `config/theme.ts`; legacy screens keep the light-only `colors`. Pure list logic (merge/sort, previews, ring states) lives in `@lightbulb/shared` (`messaging/chatList.ts`, vitest). Phone-only state (pins, archived sessions, the running-sessions switch) lives in `apps/mobile/src/lib/chats.ts` over the existing `LocalStore`. UI is a shared `ListRow`, a palette-aware `Avatar` with state rings and vendor marks, `ChatRow`/`PinnedStrip`, and a new `(tabs)/chats.tsx`; Teams and Settings move into the tab bar.

**Tech Stack:** Expo SDK 57 / React Native 0.86, expo-router `NativeTabs` + native Stack, `@expo/ui` SwiftUI (`Form`, `Toggle`), `react-native-gesture-handler` `ReanimatedSwipeable`, `expo-symbols`, `expo-image`, jest-expo + @testing-library/react-native (mobile), vitest (shared).

**Spec:** `docs/superpowers/specs/2026-09-26-iphone-messaging-redesign-design.md` (binding). Visual reference only: `.superpowers/brainstorm/73529-1790476059/content/chats-list.html` option C.

## Global Constraints

- **JS/TS only.** No new dependency, no native module, no change to `apps/mobile/app.json`, `apps/mobile/package.json` or `pnpm-lock.yaml`. The OTA fingerprint must not move (Task 12 checks it).
- **Tokens.** New surfaces read `useChatPalette()` from `apps/mobile/src/config/theme.ts`. The values are copied one-for-one from `apps/frontend/src/app/globals.css` `:root[data-chat-theme]` / `:root[data-chat-theme="dark"]`. No other colour literal may be introduced outside `theme.ts`:

  | key (web token) | light | dark |
  |---|---|---|
  | `rail` (`--plane-rail`) | `#DEDEDE` | `#101012` |
  | `sidebar` (`--plane-sidebar`) | `#EFEFEF` | `#171719` |
  | `pane` (`--plane-pane`) | `#FFFFFF` | `#1E1E21` |
  | `raised` (`--plane-raised`) | `#F7F7F7` | `#27272B` |
  | `edge` (`--plane-edge`) | `#E2E2E2` | `#323236` |
  | `selected` (`--plane-selected`) | `#E4E4E6` | `#2E2E33` |
  | `hover` (`--plane-hover`) | `#F2F2F3` | `#26262A` |
  | `fg` (`--fg`) | `#1A1A1C` | `#ECECEE` |
  | `fgSoft` (`--fg-soft`) | `#52525A` | `#B4B4B8` |
  | `fgMute` (`--fg-mute`) | `#606069` | `#97979C` |
  | `link` (`--link`) | `#1E52CC` | `#8FB0FF` |
  | `onLink` (`--on-link`) | `#FFFFFF` | `#0C121A` |
  | `alert` (`--alert`) | `#A83C29` | `#E7806F` |
  | `live` (`--live`) | `#186D48` | `#4BC08A` |
  | `warn` (`--warn` = `--brand`) | `#52525A` | `#B4B4B8` |
  | `agentTint` (`--agent-tint-*`, all three equal) | `#6E6E78` | `#9B9BA2` |
  | `onAgentTint` (`--on-agent-tint`) | `#FFFFFF` | `#101012` |

  Four semantic aliases, each an existing value above (no new colours): `ground` = light `sidebar` `#EFEFEF` / dark `rail` `#101012` (conversation and grouped-list background); `surface` = `pane` (rows, bubbles, cards); `chip` = light `selected` `#E4E4E6` / dark `raised` `#27272B`; `onFg` = light `pane` `#FFFFFF` / dark `rail` `#101012` (text on an `fg`-filled "mine" bubble or primary button).
- **Legacy-token mapping** when a redesigned surface replaces a `colors.*` read: `cream`/`white` → `surface` (page lists) or `ground` (grouped/conversation backgrounds); `ink`/`assistantInk` → `fg`; `inkSoft` → `fgSoft`; `mute`/`muted`/`muteSoft`/`placeholder` → `fgMute`; `edge`/`border` → `edge`; `creamDeeper`/`chip`/`codeBg`/`skeleton` → `raised`; `rowPressed` → `selected`; `forest` (buttons, send) → `fg` with `onFg` content; `forest` (text links) and `info` → `link`; `errorText`/`danger` → `alert`; `runningGreen`/`green`/`forestSoft` → `live`; `warn` → `warn`.
- **Contrast:** every text token clears 4.5:1 on every plane it can sit on (`pane`, `sidebar`, `raised`, `selected`, `rail`), both schemes — enforced by the Task 1 test, same rule as web `chatThemeContrast.test.ts`.
- **Fonts** (from `theme.ts` `fonts`): content `SourceSans3_400Regular` / `SourceSans3_500Medium` / `SourceSans3_600SemiBold`; display titles `Lora_500Medium` (the large "Chats" title); agent monograms keep `Lora_700Bold_Italic`; navigation chrome is the system font (`type.navTitle`, no `fontFamily`). Message/preview body 16pt where it is a message; metadata ≥ 12pt.
- **No monospace except code.** `fonts.mono` (Menlo) only for code blocks and inline `code` spans. Nothing in this plan introduces mono.
- **Avatars:** rounded squares, radius `radii.lg` (12) when size > 32 else `radii.md` (8); never circles. Agent tile fill `agentTint`, initials `onAgentTint`; human tile fill `selected`, initials `fg`. State ring = 2pt stroke drawn 2pt outside the tile: `running` → `live`, `attention` → `alert`, `paused` → `warn`. Presence dot bottom-right (agent → `link`, online human → `live`), ringed in `surface`. Terminal badge bottom-left when a human is sharing a session. Claude Code / Codex render the existing PNG marks (`SessionToolIcon`); no emoji and no SF-symbol fallback for a known tool.
- **`@lightbulb/shared` stays framework-neutral** (no react-native, no DOM, no `next/*`); new shared logic ships with vitest tests.
- **Accessibility:** VoiceOver label on every row, pin, ring and swipe-equivalent action; 44pt minimum touch targets (use `hitSlop` for smaller glyphs); fixed-size tiles set `maxFontSizeMultiplier={1}`; list text scales with Dynamic Type.
- **Storage:** per-device keys are `lightbulb.*` in the existing `LocalStore` (`expo-file-system`), never AsyncStorage. Pins key: `lightbulb.pins.<orgId>` (ids only).
- **Test commands (targeted only, never the full suites):** mobile `cd apps/mobile && pnpm jest <paths>` (escape parentheses: `'src/app/\(app\)/...'`), `cd apps/mobile && pnpm typecheck`, `cd apps/mobile && pnpm lint`; shared `cd packages/shared && pnpm test <paths>`.
- **Commits** end with the line `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **A stored pin whose target is gone** (agent removed from the workspace, room left or deleted) — the pin is silently not drawn and nothing crashes; the stored list is only rewritten on the reader's next edit. Pinned by `buildPinnedItems` "drops unknown targets" (Task 5).
2. **Corrupt or foreign data in the pins store** (non-array, wrong prefix, numbers) — treated as "never edited", so the defaults show. Pinned by `readPins` "rejects malformed data" (Task 5).
3. **Switching workspace** — one workspace's pins and archived sessions never show in another. Pinned by `readPins` "scopes by org" (Task 5).
4. **Rooms and sessions with missing or unparseable timestamps** (`lastMessageAt: null`, no `lastEventAt`) — sorted to the bottom deterministically, never `NaN` ordering. Pinned by `mergeChats` "orders missing timestamps last, stably" (Task 2).
5. **First run before the roster answers** (`/teams/agents` slow or failing) — the strip is empty rather than wrong, nothing is written to disk, and the starter agents appear as soon as the roster lands. Pinned by `resolvePins` + the Chats screen "defaults arrive with the roster" test (Tasks 5 and 9).

**Known data limits (spec gaps, resolved by degrading, no backend change):** typing and run frames only arrive on the one subscribed room channel, so a Chats row can show a live "working…" line only for an agent DM whose roster status is running and for a busy session; human "X is typing…" in the list is not available and is not drawn. Humans carry no presence on mobile (`RoomMember` has no `online`), so the green online dot stays dark.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/mobile/src/config/theme.ts` (modify) | add `chatPalette`, `ChatPalette`, `useChatPalette()` |
| `apps/mobile/src/config/__tests__/chatPalette.test.ts` (create) | values + AA contrast on every plane, both schemes |
| `apps/mobile/src/app/_layout.tsx` (modify) | `StatusBar style="auto"` |
| `packages/shared/src/messaging/chatList.ts` (create) | `mergeChats`, `roomPreview`, `sessionPreview`, `agentRingState`, `sessionRingState`, types |
| `packages/shared/src/messaging/chatList.test.ts` (create) | vitest |
| `packages/shared/src/index.ts` (modify) | exports |
| `apps/mobile/src/components/room/Avatar.tsx` (modify) | palette, agent tint, rings, vendor marks |
| `apps/mobile/src/components/session/SessionToolIcon.tsx` (modify) | `tint` prop |
| `apps/mobile/src/components/ui/ListRow.tsx` (create) | the one list-row anatomy (Chats now, Activity in Part 3) |
| `apps/mobile/src/lib/chats.ts` (create) | pins / archived sessions / switch storage, `otherMembers`, `pinKeyForRoom`, `buildPinnedItems` |
| `apps/mobile/src/lib/workspaceRoster.ts` (create) | `useWorkspaceRoster` (agents with status + members) |
| `apps/mobile/src/lib/teamsData.ts` (modify) | `TeamsAgent.status` / `role` fields |
| `apps/mobile/src/components/chats/ChatRow.tsx` (create) | `RoomChatRow`, `SessionChatRow` (swipes, menus) |
| `apps/mobile/src/components/chats/PinnedStrip.tsx` (create) | the strip, unpin, reorder mode |
| `apps/mobile/src/lib/tabHeader.ts` (create) | `useTabHeader` — a tab writes the shared (app) header while selected |
| `apps/mobile/src/app/(app)/(tabs)/chats.tsx` (create) | the Chats tab |
| `apps/mobile/src/app/(app)/(tabs)/_layout.tsx` (modify) | four tabs |
| `apps/mobile/src/app/(app)/(tabs)/teams.tsx` (moved from `teams/index.tsx`) | Teams tab |
| `apps/mobile/src/app/(app)/(tabs)/settings.tsx` (moved from `settings.tsx`) | Settings tab + More's rows + switch |
| deleted | `(tabs)/home.tsx`, `(tabs)/dms.tsx`, `(tabs)/more.tsx`, `(app)/sessions.tsx`, `components/room/RoomRow.tsx` and their tests |

---

### Task 1: Chat palette (light + dark) and status bar

**Files:**
- Modify: `apps/mobile/src/config/theme.ts` (append after `radii`)
- Modify: `apps/mobile/src/app/_layout.tsx:90`
- Test: `apps/mobile/src/config/__tests__/chatPalette.test.ts`

**Interfaces:**
- Produces: `chatPalette: { light: ChatPalette; dark: ChatPalette }`, `type ChatPalette`, `useChatPalette(): ChatPalette`.

- [ ] **Step 1: Write the failing test**

```ts
// apps/mobile/src/config/__tests__/chatPalette.test.ts
import { chatPalette } from "../theme";

function channel(byte: number): number {
  const c = byte / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}
function luminance(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  return 0.2126 * channel((n >> 16) & 0xff) + 0.7152 * channel((n >> 8) & 0xff) + 0.0722 * channel(n & 0xff);
}
function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

const TEXT = ["fg", "fgSoft", "fgMute", "link", "alert", "live", "warn"] as const;
const PLANES = ["pane", "sidebar", "raised", "selected", "rail"] as const;

describe("chatPalette", () => {
  it("mirrors web [data-chat-theme] exactly", () => {
    expect(chatPalette.light).toMatchObject({
      rail: "#DEDEDE", sidebar: "#EFEFEF", pane: "#FFFFFF", raised: "#F7F7F7", edge: "#E2E2E2",
      selected: "#E4E4E6", hover: "#F2F2F3", fg: "#1A1A1C", fgSoft: "#52525A", fgMute: "#606069",
      link: "#1E52CC", onLink: "#FFFFFF", alert: "#A83C29", live: "#186D48", warn: "#52525A",
      agentTint: "#6E6E78", onAgentTint: "#FFFFFF",
      ground: "#EFEFEF", surface: "#FFFFFF", chip: "#E4E4E6", onFg: "#FFFFFF",
    });
    expect(chatPalette.dark).toMatchObject({
      rail: "#101012", sidebar: "#171719", pane: "#1E1E21", raised: "#27272B", edge: "#323236",
      selected: "#2E2E33", hover: "#26262A", fg: "#ECECEE", fgSoft: "#B4B4B8", fgMute: "#97979C",
      link: "#8FB0FF", onLink: "#0C121A", alert: "#E7806F", live: "#4BC08A", warn: "#B4B4B8",
      agentTint: "#9B9BA2", onAgentTint: "#101012",
      ground: "#101012", surface: "#1E1E21", chip: "#27272B", onFg: "#101012",
    });
  });

  it.each(["light", "dark"] as const)("%s clears AA for every text token on every plane", (scheme) => {
    const p = chatPalette[scheme];
    const failures = TEXT.flatMap((t) =>
      PLANES.filter((plane) => contrast(p[t], p[plane]) < 4.5).map((plane) => `${t} on ${plane}`),
    );
    expect(failures).toEqual([]);
  });

  it.each(["light", "dark"] as const)("%s: text on a mine bubble and on an agent tile clears AA", (scheme) => {
    const p = chatPalette[scheme];
    expect(contrast(p.onFg, p.fg)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(p.onAgentTint, p.agentTint)).toBeGreaterThanOrEqual(4.5);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/mobile && pnpm jest src/config/__tests__/chatPalette.test.ts`
Expected: FAIL — `chatPalette` is undefined.

- [ ] **Step 3: Implement**

Append to `apps/mobile/src/config/theme.ts` (and add `useColorScheme` to the existing `react-native` import line: `import { Platform, useColorScheme } from "react-native";`):

```ts
/**
 * The messaging palette: web `globals.css` `:root[data-chat-theme]` and its
 * `="dark"` override, one-for-one (the iPhone redesign's tokens). `colors`
 * above stays the light-only palette the not-yet-redesigned screens read.
 *
 * Four aliases name ROLES, never new colours — each is a value already in the
 * same scheme: `ground` (conversation/grouped-list background: light sidebar,
 * dark rail), `surface` (rows, bubbles, cards: pane), `chip` (hanging chips:
 * light selected, dark raised) and `onFg` (text on an fg-filled bubble or
 * button: light pane, dark rail).
 *
 * Contrast: every text token is checked against EVERY plane
 * (`__tests__/chatPalette.test.ts`), the web rule.
 */
const chatLight = {
  rail: "#DEDEDE",
  sidebar: "#EFEFEF",
  pane: "#FFFFFF",
  raised: "#F7F7F7",
  edge: "#E2E2E2",
  selected: "#E4E4E6",
  hover: "#F2F2F3",
  fg: "#1A1A1C",
  fgSoft: "#52525A",
  fgMute: "#606069",
  link: "#1E52CC",
  onLink: "#FFFFFF",
  alert: "#A83C29",
  live: "#186D48",
  warn: "#52525A",
  agentTint: "#6E6E78",
  onAgentTint: "#FFFFFF",
  ground: "#EFEFEF",
  surface: "#FFFFFF",
  chip: "#E4E4E6",
  onFg: "#FFFFFF",
} as const;

export type ChatPalette = { readonly [K in keyof typeof chatLight]: string };

export const chatPalette: { light: ChatPalette; dark: ChatPalette } = {
  light: chatLight,
  dark: {
    rail: "#101012",
    sidebar: "#171719",
    pane: "#1E1E21",
    raised: "#27272B",
    edge: "#323236",
    selected: "#2E2E33",
    hover: "#26262A",
    fg: "#ECECEE",
    fgSoft: "#B4B4B8",
    fgMute: "#97979C",
    link: "#8FB0FF",
    onLink: "#0C121A",
    alert: "#E7806F",
    live: "#4BC08A",
    warn: "#B4B4B8",
    agentTint: "#9B9BA2",
    onAgentTint: "#101012",
    ground: "#101012",
    surface: "#1E1E21",
    chip: "#27272B",
    onFg: "#101012",
  },
};

/** The messaging palette for the phone's current appearance (app.json is `automatic`). */
export function useChatPalette(): ChatPalette {
  return useColorScheme() === "dark" ? chatPalette.dark : chatPalette.light;
}
```

In `apps/mobile/src/app/_layout.tsx` change `<StatusBar style="dark" />` to `<StatusBar style="auto" />` (a pinned dark status bar is unreadable over the dark Chats list).

Also update the file-header sentence in `theme.ts` that says "The web's `.dark` block has no mobile counterpart yet" to: "Dark mode ships through `chatPalette` / `useChatPalette()` below for the redesigned messaging surfaces; `colors` stays light-only for the rest."

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/mobile && pnpm jest src/config/__tests__/chatPalette.test.ts src/config/__tests__/theme.test.ts`
Expected: PASS (both files).

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/config/theme.ts apps/mobile/src/config/__tests__/chatPalette.test.ts apps/mobile/src/app/_layout.tsx
git commit -m "feat(mobile): light + dark chat palette from web [data-chat-theme]

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Shared chat-list logic (merge, previews, ring states)

**Files:**
- Create: `packages/shared/src/messaging/chatList.ts`
- Create: `packages/shared/src/messaging/chatList.test.ts`
- Modify: `packages/shared/src/index.ts` (after the `localSessionLabels` export on line 197)

**Interfaces:**
- Consumes: `sessionArchived`, `sessionStatus` (`./localSessionLabels`), `LocalSession`, `RoomSummary` (`./types`).
- Produces:
  - `type ChatEntry = { kind: "room"; key: string; at: number; room: RoomSummary } | { kind: "session"; key: string; at: number; session: LocalSession }`
  - `type AvatarRing = "running" | "attention" | "paused"`
  - `roomActivityAt(room): number`, `sessionActivityAt(session): number`
  - `mergeChats(rooms, sessions, { archivedSessions?: Readonly<Record<string, number>>; now?: number }): ChatEntry[]`
  - `roomPreview(room: Pick<RoomSummary, "kind" | "lastMessage">, viewerId: string | null | undefined): string`
  - `sessionPreview(session: Pick<LocalSession, "status" | "lastAssistantMessage">): string`
  - `agentRingState(status: string | null | undefined): AvatarRing | null`
  - `sessionRingState(session: Pick<LocalSession, "status">): AvatarRing | null`

- [ ] **Step 1: Write the failing test**

```ts
// packages/shared/src/messaging/chatList.test.ts
import { describe, expect, it } from "vitest";

import {
  agentRingState, mergeChats, roomPreview, sessionPreview, sessionRingState,
} from "./chatList";
import type { LocalSession, RoomSummary } from "./types";

const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r", name: null, kind: "channel", roomRevision: 1, lastMessageSeq: 1,
  lastMessageAt: "2026-09-26T10:00:00Z", unreadCount: 0, updatedAt: "2026-09-26T10:00:00Z",
  topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, ...over,
});
const session = (over: Partial<LocalSession>): LocalSession => ({
  sessionId: "s", deviceId: "d", humanUserId: "u", provider: "claude", generation: "g",
  status: "idle", providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 5, lastEventAt: "2026-09-26T09:00:00Z", ...over,
});
const NOW = Date.parse("2026-09-26T12:00:00Z");

describe("mergeChats", () => {
  it("interleaves rooms and sessions newest first", () => {
    const entries = mergeChats(
      [room({ roomId: "a", lastMessageAt: "2026-09-26T08:00:00Z" }), room({ roomId: "b", lastMessageAt: "2026-09-26T11:00:00Z" })],
      [session({ sessionId: "s1", lastEventAt: "2026-09-26T10:00:00Z" })],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["room:b", "session:s1", "room:a"]);
  });

  it("falls back to updatedAt for a room and endedAt for a session", () => {
    const entries = mergeChats(
      [room({ roomId: "a", lastMessageAt: null, updatedAt: "2026-09-26T11:30:00Z" })],
      [session({ sessionId: "s1", lastEventAt: null, endedAt: "2026-09-26T11:00:00Z", status: "offline" })],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["room:a", "session:s1"]);
  });

  it("orders missing timestamps last, stably", () => {
    const entries = mergeChats(
      [room({ roomId: "z", lastMessageAt: null, updatedAt: "not a date" }), room({ roomId: "y", lastMessageAt: null, updatedAt: "" }), room({ roomId: "x" })],
      [session({ sessionId: "s1", lastEventAt: undefined, endedAt: undefined })],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["room:x", "room:y", "room:z", "session:s1"]);
    expect(entries.every((e) => Number.isFinite(e.at))).toBe(true);
  });

  it("drops deleted rooms, ended sessions and sessions offline for three days", () => {
    const entries = mergeChats(
      [room({ roomId: "gone", deletedAt: "2026-09-26T09:00:00Z" })],
      [
        session({ sessionId: "ended", status: "ended" }),
        session({ sessionId: "stale", status: "offline", lastEventAt: "2026-09-20T00:00:00Z" }),
        session({ sessionId: "live", status: "busy" }),
      ],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["session:live"]);
  });

  it("hides a session archived on this device until it has new output", () => {
    const quiet = session({ sessionId: "s1", latestEventSeq: 5 });
    expect(mergeChats([], [quiet], { archivedSessions: { s1: 5 }, now: NOW })).toEqual([]);
    const moved = session({ sessionId: "s1", latestEventSeq: 6 });
    expect(mergeChats([], [moved], { archivedSessions: { s1: 5 }, now: NOW }).map((e) => e.key)).toEqual(["session:s1"]);
  });
});

describe("roomPreview", () => {
  const last = { messageId: "m", seq: 3, authorId: "u2", authorKind: "human" as const, authorName: "Leo", textPreview: "can someone check the payout?", hasAttachments: false, createdAt: "x", threadRootMessageId: null };
  it("prefixes the author in a channel", () => {
    expect(roomPreview({ kind: "channel", lastMessage: last }, "me")).toBe("Leo: can someone check the payout?");
  });
  it("leaves a 1:1 DM's counterpart unprefixed", () => {
    expect(roomPreview({ kind: "dm", lastMessage: last }, "me")).toBe("can someone check the payout?");
  });
  it("says You for the viewer's own message, in any room", () => {
    expect(roomPreview({ kind: "dm", lastMessage: { ...last, authorId: "me" } }, "me")).toBe("You: can someone check the payout?");
  });
  it("names an attachment-only message", () => {
    expect(roomPreview({ kind: "channel", lastMessage: { ...last, textPreview: " ", hasAttachments: true } }, "me")).toBe("Leo: Attachment");
  });
  it("has copy for an empty room", () => {
    expect(roomPreview({ kind: "channel", lastMessage: null }, "me")).toBe("No messages yet");
  });
});

describe("sessionPreview", () => {
  it("shows the last reply, else the status word", () => {
    expect(sessionPreview({ status: "idle", lastAssistantMessage: " All green. " })).toBe("All green.");
    expect(sessionPreview({ status: "offline", lastAssistantMessage: null })).toBe("Offline");
  });
  it("puts a waiting prompt ahead of the last reply", () => {
    expect(sessionPreview({ status: "waiting-approval", lastAssistantMessage: "Done" })).toBe("Waiting for input");
  });
});

describe("ring states", () => {
  it("rings only real states; a never-run 'active' agent is not working", () => {
    expect(agentRingState("running")).toBe("running");
    expect(agentRingState("Thinking")).toBe("running");
    expect(agentRingState("stalled")).toBe("running");
    expect(agentRingState("error")).toBe("attention");
    expect(agentRingState("failed")).toBe("attention");
    expect(agentRingState("paused")).toBe("paused");
    expect(agentRingState("active")).toBeNull();
    expect(agentRingState("idle")).toBeNull();
    expect(agentRingState(undefined)).toBeNull();
  });
  it("rings a busy session live and a waiting one for attention", () => {
    expect(sessionRingState({ status: "busy" })).toBe("running");
    expect(sessionRingState({ status: "waiting-approval" })).toBe("attention");
    expect(sessionRingState({ status: "idle" })).toBeNull();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd packages/shared && pnpm test src/messaging/chatList.test.ts`
Expected: FAIL — cannot resolve `./chatList`.

- [ ] **Step 3: Implement**

```ts
// packages/shared/src/messaging/chatList.ts
/**
 * The phone's Chats list: every room the viewer is in and every live workspace
 * session, as ONE list sorted by last activity, plus the row's preview line and
 * the avatar's state ring. Pure: no React, no DOM, no react-native.
 */
import { sessionArchived, sessionStatus } from "./localSessionLabels";
import type { LocalSession, RoomSummary } from "./types";

export type ChatEntry =
  | { kind: "room"; key: string; at: number; room: RoomSummary }
  | { kind: "session"; key: string; at: number; session: LocalSession };

/** Web `.participant-avatar[data-agent-state]`. */
export type AvatarRing = "running" | "attention" | "paused";

/** Epoch ms, 0 for an absent or unparseable value (sorts last, never NaN). */
const epoch = (value: string | null | undefined): number => (value ? Date.parse(value) || 0 : 0);

export const roomActivityAt = (room: Pick<RoomSummary, "lastMessageAt" | "updatedAt">): number =>
  epoch(room.lastMessageAt) || epoch(room.updatedAt);

export const sessionActivityAt = (session: Pick<LocalSession, "lastEventAt" | "endedAt">): number =>
  epoch(session.lastEventAt) || epoch(session.endedAt);

export interface MergeChatsOptions {
  /** Sessions the viewer archived on this device → the `latestEventSeq` they had then. */
  archivedSessions?: Readonly<Record<string, number>>;
  now?: number;
}

/**
 * Rooms (minus deleted ones) and sessions (minus ended, three-day-offline and
 * locally archived ones — an archived session comes back once it has new
 * output), newest activity first; equal times order by key so the list never
 * shuffles between renders.
 */
export function mergeChats(
  rooms: readonly RoomSummary[],
  sessions: readonly LocalSession[],
  { archivedSessions = {}, now = Date.now() }: MergeChatsOptions = {},
): ChatEntry[] {
  const entries: ChatEntry[] = [];
  for (const room of rooms) {
    if (room.deletedAt) continue;
    entries.push({ kind: "room", key: `room:${room.roomId}`, at: roomActivityAt(room), room });
  }
  for (const session of sessions) {
    if (session.status === "ended" || sessionArchived(session, now)) continue;
    const archivedAt = archivedSessions[session.sessionId];
    if (archivedAt !== undefined && (session.latestEventSeq ?? 0) <= archivedAt) continue;
    entries.push({ kind: "session", key: `session:${session.sessionId}`, at: sessionActivityAt(session), session });
  }
  return entries.sort((a, b) => b.at - a.at || a.key.localeCompare(b.key));
}

/** "Leo: text" in a channel or group, "text" from a DM's counterpart, "You: text" for mine. */
export function roomPreview(
  room: Pick<RoomSummary, "kind" | "lastMessage">,
  viewerId: string | null | undefined,
): string {
  const last = room.lastMessage;
  if (!last) return "No messages yet";
  const body = last.textPreview.trim() || (last.hasAttachments ? "Attachment" : "");
  const mine = last.authorKind === "human" && last.authorId === viewerId;
  if (mine) return `You: ${body}`;
  return room.kind === "dm" ? body : `${last.authorName}: ${body}`;
}

/** A waiting prompt first, else the last reply, else the status word. */
export function sessionPreview(session: Pick<LocalSession, "status" | "lastAssistantMessage">): string {
  if (session.status === "waiting-approval") return sessionStatus["waiting-approval"];
  return session.lastAssistantMessage?.trim() || sessionStatus[session.status];
}

/**
 * Paperclip's agent status → ring. `active` (the created-but-never-run default;
 * the heartbeat moves an agent to `running` and back to `idle`) and `online`
 * deliberately draw NO ring: a green ring on every never-run agent would say
 * all six starters are working. (Web's `AGENT_STATUS_CLASS` counts them as
 * live; this is a phone-side choice, web is unchanged.)
 */
const RING_BY_STATUS: Readonly<Record<string, AvatarRing>> = {
  running: "running",
  thinking: "running",
  covered: "running",
  stalled: "running",
  error: "attention",
  failed: "attention",
  paused: "paused",
};

export const agentRingState = (status: string | null | undefined): AvatarRing | null =>
  (status ? RING_BY_STATUS[status.toLowerCase()] : undefined) ?? null;

export const sessionRingState = (session: Pick<LocalSession, "status">): AvatarRing | null =>
  session.status === "busy" ? "running" : session.status === "waiting-approval" ? "attention" : null;
```

In `packages/shared/src/index.ts`, directly after the `localSessionLabels` export line, add:

```ts
export { agentRingState, mergeChats, roomActivityAt, roomPreview, sessionActivityAt, sessionPreview, sessionRingState } from "./messaging/chatList";
export type { AvatarRing, ChatEntry, MergeChatsOptions } from "./messaging/chatList";
```

- [ ] **Step 4: Run the tests**

Run: `cd packages/shared && pnpm test src/messaging/chatList.test.ts && pnpm typecheck`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src/messaging/chatList.ts packages/shared/src/messaging/chatList.test.ts packages/shared/src/index.ts
git commit -m "feat(shared): merged chat list, previews and ring states for the phone

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Avatar — palette, agent tint, state rings, vendor marks

**Files:**
- Modify: `apps/mobile/src/components/room/Avatar.tsx`
- Modify: `apps/mobile/src/components/session/SessionToolIcon.tsx`
- Test: `apps/mobile/src/components/room/__tests__/Avatar.test.tsx` (extend; keep existing cases)

**Interfaces:**
- Consumes: `ChatPalette`, `chatPalette` (Task 1); `AvatarRing` (Task 2).
- Produces (all new props optional, so every existing caller is unchanged):
  ```ts
  Avatar(props: {
    name: string; kind: "human" | "agent" | "system"; emoji?: string; size?: number;
    online?: boolean; showPresence?: boolean; sharing?: boolean;
    ring?: AvatarRing | null;   // 2pt ring 2pt outside the tile
    vendor?: string;            // "claude" | "codex" | other → SessionToolIcon mark instead of initials
    palette?: ChatPalette;      // default chatPalette.light
  })
  SessionToolIcon(props: { provider: string; size?: number; tint?: string })  // tint default colors.ink (codex template + unknown)
  ```
  Ring accessibility labels: `running` "Working", `attention` "Needs attention", `paused` "Paused".

- [ ] **Step 1: Write the failing tests** (append to `Avatar.test.tsx`; add `import { StyleSheet } from "react-native";` and `import { chatPalette } from "../../../config/theme";` at the top if absent)

```tsx
describe("Avatar redesign", () => {
  it("fills an agent tile with the agent tint and draws the initial in its foreground", async () => {
    await render(<Avatar name="Growth" kind="agent" size={56} palette={chatPalette.dark} />);
    const initial = screen.getByText("G");
    expect(StyleSheet.flatten(initial.props.style).color).toBe(chatPalette.dark.onAgentTint);
    const tile = screen.getByTestId("avatar-tile");
    expect(StyleSheet.flatten(tile.props.style)).toMatchObject({ backgroundColor: chatPalette.dark.agentTint, borderRadius: 12 });
  });

  it.each([
    ["running", "Working", "live"],
    ["attention", "Needs attention", "alert"],
    ["paused", "Paused", "warn"],
  ] as const)("rings %s in the %s colour", async (ring, label, token) => {
    await render(<Avatar name="CEO" kind="agent" ring={ring} palette={chatPalette.light} />);
    const node = screen.getByLabelText(label);
    expect(StyleSheet.flatten(node.props.style).borderColor).toBe(chatPalette.light[token]);
  });

  it("draws no ring by default", async () => {
    await render(<Avatar name="CEO" kind="agent" />);
    expect(screen.queryByLabelText("Working")).toBeNull();
  });

  it("renders a coding tool's mark instead of initials", async () => {
    await render(<Avatar name="Claude" kind="agent" vendor="claude" size={52} />);
    expect(screen.getByTestId("session-tool-claude")).toBeTruthy();
    expect(screen.queryByText("C")).toBeNull();
  });

  it("keeps rounded squares, never circles", async () => {
    await render(<Avatar name="Ann Lee" kind="human" size={28} />);
    expect(StyleSheet.flatten(screen.getByTestId("avatar-tile").props.style).borderRadius).toBe(8);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/components/room/__tests__/Avatar.test.tsx`
Expected: FAIL — no `avatar-tile` testID, no ring labels.

- [ ] **Step 3: Implement**

Rewrite `Avatar.tsx` keeping every existing prop and behaviour, with these changes:
- Import `chatPalette, type ChatPalette` and `type AvatarRing` (from `@lightbulb/shared`), plus `SessionToolIcon` from `../session/SessionToolIcon`.
- `const p = palette ?? chatPalette.light;` `const radius = size > 32 ? radii.lg : radii.md;`
- The tile `View` gets `testID="avatar-tile"` and style `{ width: size, height: size, borderRadius: radius, backgroundColor: vendor ? p.surface : kind === "agent" ? p.agentTint : p.selected }`, plus `borderWidth: StyleSheet.hairlineWidth, borderColor: p.edge` when `vendor`.
- Content: `vendor` → `<SessionToolIcon provider={vendor} size={Math.round(size * 0.55)} tint={p.fg} />`; else emoji as today; else initials with `color: kind === "agent" ? p.onAgentTint : p.fg` (fonts unchanged: agent `fonts.loraBoldItalic`, human `fonts.sansSemiBold`, `maxFontSizeMultiplier={1}`).
- Ring (rendered before the tile so the tile paints over nothing it needs):
  ```tsx
  const RING_LABEL: Record<AvatarRing, string> = { running: "Working", attention: "Needs attention", paused: "Paused" };
  const RING_COLOR: Record<AvatarRing, keyof ChatPalette> = { running: "live", attention: "alert", paused: "warn" };
  {ring ? (
    <View
      accessible
      accessibilityLabel={RING_LABEL[ring]}
      style={{ position: "absolute", top: -4, left: -4, right: -4, bottom: -4, borderRadius: radius + 4, borderWidth: 2, borderColor: p[RING_COLOR[ring]] }}
    />
  ) : null}
  ```
- Presence dot and sharing badge: replace `colors.presenceAgent` → `p.link`, `colors.presenceOnline` → `p.live`, `colors.cream` border → `p.surface`, sharing badge fill `colors.forest` → `p.fg` with the `terminal` symbol tinted `p.onFg`.
- Delete the now-unused `colors`/`fonts` style entries that referenced `forestTint`/`forest`.

In `SessionToolIcon.tsx` add `tint = colors.ink` to the props and use it for the codex template tint and the unknown-tool `terminal` symbol (`tintColor={tint}`), so a dark tile never draws a near-black mark.

- [ ] **Step 4: Run the tests**

Run: `cd apps/mobile && pnpm jest src/components/room/__tests__/Avatar.test.tsx src/components/room/__tests__/RoomRow.test.tsx src/components/room/__tests__/MessageGroup.test.tsx && pnpm typecheck`
Expected: PASS (the existing callers still render with the light default).

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/room/Avatar.tsx apps/mobile/src/components/session/SessionToolIcon.tsx apps/mobile/src/components/room/__tests__/Avatar.test.tsx
git commit -m "feat(mobile): avatar tint, state rings and vendor marks

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: ListRow — the one list-row anatomy

**Files:**
- Create: `apps/mobile/src/components/ui/ListRow.tsx`
- Test: `apps/mobile/src/components/ui/__tests__/ListRow.test.tsx`

**Interfaces:**
- Consumes: `ChatPalette` (Task 1), `fonts`, `spacing` from theme.
- Produces:
  ```ts
  export interface ListRowProps {
    palette: ChatPalette;
    leading: ReactNode;                 // an Avatar (52pt Chats, 36pt Activity)
    title: string;
    /** Small words after the title: "Draft", "Shared", a bell.slash glyph. */
    titleAccessory?: ReactNode;
    time?: string;
    preview?: string | null;
    /** "live" = link colour (typing… / working…), "draft" = alert colour prefix row. */
    previewTone?: "normal" | "live";
    previewLines?: 1 | 2;               // default 2
    unread?: boolean;                   // blue dot in the left gutter + semibold title
    trailing?: ReactNode;               // Part 3: Activity's one action button
    onPress?: () => void;               // Link.Trigger injects this
    onLongPress?: () => void;
    testID?: string;
    accessibilityLabel: string;
    accessibilityHint?: string;
    accessibilityActions?: PressableProps["accessibilityActions"];
    onAccessibilityAction?: PressableProps["onAccessibilityAction"];
  }
  export function ListRow(props: ListRowProps): JSX.Element
  ```
- Behaviour: a `Pressable` row, `backgroundColor: palette.surface`, pressed `palette.selected`; `paddingLeft: 20` (the unread gutter), `paddingRight: spacing.lg`, `paddingVertical: 10`, `minHeight: 44`, `gap: spacing.md`. Unread dot: 9pt circle `palette.link` absolutely at `left: 6`, vertically centred, `accessibilityLabel="Unread"` on the dot. Text column: first line = title (`fonts.sans` 16/21, `palette.fg`, `fonts.sansSemiBold` when unread, `numberOfLines={1}`, flex 1) + `titleAccessory` + time (`fonts.sans` 13/18, `palette.fgMute`); second block = preview (`fonts.sans` 15/20, `palette.fgMute`, `palette.link` when `previewTone === "live"`, `numberOfLines={previewLines}`). A hairline separator (`palette.edge`) under the text column only (not under the avatar), drawn as the text column's `borderBottomWidth: StyleSheet.hairlineWidth` with `paddingBottom: 10`. `accessibilityRole="button"`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/ui/__tests__/ListRow.test.tsx
import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet, Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { ListRow } from "../ListRow";

const p = chatPalette.light;

describe("ListRow", () => {
  it("shows title, time and a two-line preview", async () => {
    await render(
      <ListRow palette={p} leading={<Text>AV</Text>} title="CEO" time="9:12" preview="Prod deploy landed" accessibilityLabel="CEO" />,
    );
    expect(screen.getByText("CEO")).toBeTruthy();
    expect(screen.getByText("9:12")).toBeTruthy();
    expect(screen.getByText("Prod deploy landed").props.numberOfLines).toBe(2);
  });

  it("marks unread with a labelled dot and a semibold title", async () => {
    await render(<ListRow palette={p} leading={null} title="general" unread accessibilityLabel="general, unread" />);
    expect(screen.getByLabelText("Unread")).toBeTruthy();
    expect(StyleSheet.flatten(screen.getByText("general").props.style).fontFamily).toBe("SourceSans3_600SemiBold");
  });

  it("draws a live preview in the link colour", async () => {
    await render(<ListRow palette={p} leading={null} title="CEO" preview="working…" previewTone="live" accessibilityLabel="CEO" />);
    expect(StyleSheet.flatten(screen.getByText("working…").props.style).color).toBe(p.link);
  });

  it("forwards press and long press, and carries its label", async () => {
    const onPress = jest.fn();
    const onLongPress = jest.fn();
    await render(<ListRow palette={p} leading={null} title="x" onPress={onPress} onLongPress={onLongPress} accessibilityLabel="Open x" />);
    await fireEvent.press(screen.getByLabelText("Open x"));
    await fireEvent(screen.getByLabelText("Open x"), "longPress");
    expect(onPress).toHaveBeenCalled();
    expect(onLongPress).toHaveBeenCalled();
  });

  it("renders a trailing accessory", async () => {
    await render(<ListRow palette={p} leading={null} title="x" trailing={<Text>Review</Text>} accessibilityLabel="x" />);
    expect(screen.getByText("Review")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/components/ui/__tests__/ListRow.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `ListRow.tsx`** exactly to the Interfaces block above (one `Pressable`; `style={({ pressed }) => [styles.row, { backgroundColor: pressed ? palette.selected : palette.surface }]}`; styles built with `StyleSheet.create` for layout only and colours applied inline from `palette`). The trailing slot renders after the text column, vertically centred; it must be a sibling inside the row, and the row's `accessibilityLabel` must not swallow it — wrap the leading+text in the `Pressable` and put `trailing` in an outer `View` row next to it so VoiceOver reaches the trailing button on its own.

- [ ] **Step 4: Run the test**

Run: `cd apps/mobile && pnpm jest src/components/ui/__tests__/ListRow.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/ui/ListRow.tsx apps/mobile/src/components/ui/__tests__/ListRow.test.tsx
git commit -m "feat(mobile): ListRow, the shared conversation/activity row

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Phone-local chat prefs and pin resolution (`lib/chats.ts`)

**Files:**
- Create: `apps/mobile/src/lib/chats.ts`
- Test: `apps/mobile/src/lib/__tests__/chats.test.ts`

**Interfaces:**
- Consumes: `LocalStore` (`./localStore`); `agentRingState`, `roomDisplayName`, `sessionToolName`, `AvatarRing`, `LocalSession`, `RoomMember`, `RoomSummary` from `@lightbulb/shared`.
- Produces:
  ```ts
  export type PinKey = `agent:${string}` | `room:${string}`;
  export type RowParticipant = Pick<RoomMember, "kind" | "displayName" | "id"> & { online?: boolean; sharing?: boolean };
  export interface RosterAgent { id: string; name?: string | null; status?: string | null; seeded?: boolean }
  export type PinnedItem =
    | { key: PinKey; kind: "agent"; agentId: string; name: string; ring: AvatarRing | null; roomId: string | null }
    | { key: PinKey; kind: "room"; room: RoomSummary; name: string; others: RowParticipant[] }
    | { key: `session:${string}`; kind: "session"; session: LocalSession; name: string };
  pinsKey(orgId): string; archivedSessionsKey(orgId): string; RUNNING_SESSIONS_KEY: string
  readPins(orgId: string): PinKey[] | null
  writePins(orgId: string, pins: readonly PinKey[]): void
  resolvePins(stored: readonly PinKey[] | null, seededAgentIds: readonly string[]): PinKey[]
  togglePin(pins: readonly PinKey[], key: PinKey): PinKey[]
  movePin(pins: readonly PinKey[], key: PinKey, delta: -1 | 1): PinKey[]
  readShowRunningSessions(): boolean; writeShowRunningSessions(on: boolean): void
  readArchivedSessions(orgId: string): Record<string, number>
  archiveSession(orgId: string, session: Pick<LocalSession, "sessionId" | "latestEventSeq">): void
  otherMembers(room: Pick<RoomSummary, "members"> | undefined, viewerId: string | null | undefined): RowParticipant[]
  pinKeyForRoom(room: Pick<RoomSummary, "roomId" | "kind">, others: readonly Pick<RoomMember, "kind" | "id">[]): PinKey
  buildPinnedItems(input: { pins: readonly PinKey[]; rooms: readonly RoomSummary[]; agents: readonly RosterAgent[]; sessions: readonly LocalSession[]; showRunningSessions: boolean; viewerId: string | null | undefined }): PinnedItem[]
  ```

- [ ] **Step 1: Write the failing test**

```ts
// apps/mobile/src/lib/__tests__/chats.test.ts
import type { LocalSession, RoomMember, RoomSummary } from "@lightbulb/shared";

import {
  archiveSession, buildPinnedItems, movePin, pinKeyForRoom, readArchivedSessions, readPins,
  readShowRunningSessions, resolvePins, togglePin, writePins, writeShowRunningSessions, type PinKey,
} from "../chats";
import { LocalStore } from "../localStore";

const member = (over: Partial<RoomMember>): RoomMember => ({
  kind: "human", id: "me", displayName: "Me", mentionHandle: null, role: "member", listenMode: null, joinedAt: "x", joinedSeq: 0, ...over,
});
const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r", name: null, kind: "channel", roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0,
  updatedAt: "2026-09-26T10:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, ...over,
});
const session = (over: Partial<LocalSession>): LocalSession => ({
  sessionId: "s", deviceId: "d", humanUserId: "me", provider: "claude", generation: "g", status: "busy",
  providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 3, ...over,
});

describe("pins storage", () => {
  it("is null until edited, then round-trips without duplicates", () => {
    expect(readPins("org-a")).toBeNull();
    writePins("org-a", ["agent:ceo", "room:r1", "agent:ceo"]);
    expect(readPins("org-a")).toEqual(["agent:ceo", "room:r1"]);
  });

  it("scopes by org", () => {
    writePins("org-b", ["agent:x"]);
    expect(readPins("org-c")).toBeNull();
  });

  it("rejects malformed data as never-edited", () => {
    const raw = new LocalStore<Record<string, unknown>>("chats");
    raw.write({ ...raw.read(), "lightbulb.pins.org-d": ["agent:ok", 7] });
    expect(readPins("org-d")).toBeNull();
    raw.write({ ...raw.read(), "lightbulb.pins.org-d": ["channel:x"] });
    expect(readPins("org-d")).toBeNull();
    raw.write({ ...raw.read(), "lightbulb.pins.org-d": "agent:x" });
    expect(readPins("org-d")).toBeNull();
  });

  it("uses the starter agents in roster order until the reader edits", () => {
    expect(resolvePins(null, ["ceo", "eng"])).toEqual(["agent:ceo", "agent:eng"]);
    expect(resolvePins(null, [])).toEqual([]);
    expect(resolvePins(["room:r1"], ["ceo"])).toEqual(["room:r1"]);
  });

  it("toggles and moves", () => {
    const pins: PinKey[] = ["agent:a", "agent:b", "agent:c"];
    expect(togglePin(pins, "agent:b")).toEqual(["agent:a", "agent:c"]);
    expect(togglePin(pins, "room:z")).toEqual([...pins, "room:z"]);
    expect(movePin(pins, "agent:b", -1)).toEqual(["agent:b", "agent:a", "agent:c"]);
    expect(movePin(pins, "agent:c", 1)).toEqual(pins);
    expect(movePin(pins, "agent:zz", 1)).toEqual(pins);
  });
});

describe("running-sessions switch", () => {
  it("defaults on and remembers off", () => {
    expect(readShowRunningSessions()).toBe(true);
    writeShowRunningSessions(false);
    expect(readShowRunningSessions()).toBe(false);
    writeShowRunningSessions(true);
    expect(readShowRunningSessions()).toBe(true);
  });
});

describe("archived sessions", () => {
  it("records the seq a session had when archived, per org", () => {
    archiveSession("org-e", { sessionId: "s1", latestEventSeq: 9 });
    expect(readArchivedSessions("org-e")).toEqual({ s1: 9 });
    expect(readArchivedSessions("org-f")).toEqual({});
  });
});

describe("pinKeyForRoom", () => {
  it("pins a one-agent DM as the agent and everything else as the room", () => {
    expect(pinKeyForRoom({ roomId: "d1", kind: "dm" }, [{ kind: "agent", id: "ceo" }])).toBe("agent:ceo");
    expect(pinKeyForRoom({ roomId: "d2", kind: "dm" }, [{ kind: "human", id: "leo" }])).toBe("room:d2");
    expect(pinKeyForRoom({ roomId: "c1", kind: "channel" }, [])).toBe("room:c1");
  });
});

describe("buildPinnedItems", () => {
  const dm = room({ roomId: "dm-ceo", kind: "dm", members: [member({}), member({ kind: "agent", id: "ceo", displayName: "CEO" })] });
  const general = room({ roomId: "gen", name: "general" });

  it("resolves agents (with their DM and ring) and rooms, in pin order", () => {
    const items = buildPinnedItems({
      pins: ["room:gen", "agent:ceo"],
      rooms: [dm, general],
      agents: [{ id: "ceo", name: "CEO", status: "running" }],
      sessions: [], showRunningSessions: true, viewerId: "me",
    });
    expect(items.map((i) => i.key)).toEqual(["room:gen", "agent:ceo"]);
    expect(items[1]).toMatchObject({ kind: "agent", name: "CEO", ring: "running", roomId: "dm-ceo" });
  });

  it("drops unknown targets", () => {
    const items = buildPinnedItems({
      pins: ["agent:gone", "room:left"], rooms: [general], agents: [], sessions: [], showRunningSessions: true, viewerId: "me",
    });
    expect(items).toEqual([]);
  });

  it("puts busy sessions first when the switch is on, and none when off", () => {
    const busy = session({ sessionId: "s1", status: "busy" });
    const idle = session({ sessionId: "s2", status: "idle" });
    const on = buildPinnedItems({ pins: ["room:gen"], rooms: [general], agents: [], sessions: [idle, busy], showRunningSessions: true, viewerId: "me" });
    expect(on.map((i) => i.key)).toEqual(["session:s1", "room:gen"]);
    expect(on[0]).toMatchObject({ kind: "session", name: "Claude" });
    const off = buildPinnedItems({ pins: ["room:gen"], rooms: [general], agents: [], sessions: [busy], showRunningSessions: false, viewerId: "me" });
    expect(off.map((i) => i.key)).toEqual(["room:gen"]);
  });

  it("names an agent from its DM when the roster has not answered yet", () => {
    const items = buildPinnedItems({ pins: ["agent:ceo"], rooms: [dm], agents: [], sessions: [], showRunningSessions: false, viewerId: "me" });
    expect(items[0]).toMatchObject({ name: "CEO", ring: null });
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/lib/__tests__/chats.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// apps/mobile/src/lib/chats.ts
import {
  agentRingState,
  roomDisplayName,
  sessionToolName,
  type AvatarRing,
  type LocalSession,
  type RoomMember,
  type RoomSummary,
} from "@lightbulb/shared";

import { LocalStore } from "./localStore";

/**
 * The Chats tab's per-device state — pins, sessions archived from the list and
 * the "Show running sessions in pinned" switch — plus the pure resolution of
 * pins into what the strip draws. Pins are stored on the phone only (owner
 * ruling 2026-09-26: no server-side sync), ids only, one list per workspace.
 */
const store = new LocalStore<Record<string, unknown>>("chats");

export type PinKey = `agent:${string}` | `room:${string}`;
const PIN = /^(agent|room):.+$/;

export const pinsKey = (orgId: string) => `lightbulb.pins.${orgId}`;
export const archivedSessionsKey = (orgId: string) => `lightbulb.archivedSessions.${orgId}`;
export const RUNNING_SESSIONS_KEY = "lightbulb.pinRunningSessions";

function put(key: string, value: unknown): void {
  store.write({ ...store.read(), [key]: value });
}

/** This workspace's pins, or null when the reader never edited them (the strip shows the defaults). */
export function readPins(orgId: string): PinKey[] | null {
  const raw = store.read()[pinsKey(orgId)];
  if (!Array.isArray(raw) || !raw.every((key) => typeof key === "string" && PIN.test(key))) return null;
  return [...new Set(raw as PinKey[])];
}

export function writePins(orgId: string, pins: readonly PinKey[]): void {
  put(pinsKey(orgId), [...new Set(pins)]);
}

/** Stored pins, else the workspace's starter agents in roster order. Never writes. */
export const resolvePins = (stored: readonly PinKey[] | null, seededAgentIds: readonly string[]): PinKey[] =>
  stored ? [...stored] : seededAgentIds.map((id): PinKey => `agent:${id}`);

export const togglePin = (pins: readonly PinKey[], key: PinKey): PinKey[] =>
  pins.includes(key) ? pins.filter((pin) => pin !== key) : [...pins, key];

export function movePin(pins: readonly PinKey[], key: PinKey, delta: -1 | 1): PinKey[] {
  const from = pins.indexOf(key);
  const to = from + delta;
  const next = [...pins];
  if (from < 0 || to < 0 || to >= pins.length) return next;
  [next[from], next[to]] = [next[to], next[from]];
  return next;
}

/** Default on (spec): only an explicit false turns it off. */
export const readShowRunningSessions = (): boolean => store.read()[RUNNING_SESSIONS_KEY] !== false;
export const writeShowRunningSessions = (on: boolean): void => put(RUNNING_SESSIONS_KEY, on);

export function readArchivedSessions(orgId: string): Record<string, number> {
  const raw = store.read()[archivedSessionsKey(orgId)];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  return Object.fromEntries(Object.entries(raw).filter(([, seq]) => typeof seq === "number")) as Record<string, number>;
}

/** Hidden from Chats until the session has output past this seq (see `mergeChats`). */
export function archiveSession(orgId: string, session: Pick<LocalSession, "sessionId" | "latestEventSeq">): void {
  put(archivedSessionsKey(orgId), { ...readArchivedSessions(orgId), [session.sessionId]: session.latestEventSeq ?? 0 });
}

/** What a row needs of a member; `online` stays undefined (mobile has no human presence). */
export type RowParticipant = Pick<RoomMember, "kind" | "displayName" | "id"> & { online?: boolean; sharing?: boolean };

/** The room's members minus the viewer — a DM is named after the other party. */
export const otherMembers = (
  room: Pick<RoomSummary, "members"> | undefined,
  viewerId: string | null | undefined,
): RowParticipant[] => (room?.members ?? []).filter((m) => !(m.kind === "human" && m.id === viewerId));

/** A 1:1 DM with an agent pins the AGENT (it survives the DM being recreated); anything else pins the room. */
export function pinKeyForRoom(
  room: Pick<RoomSummary, "roomId" | "kind">,
  others: readonly Pick<RoomMember, "kind" | "id">[],
): PinKey {
  return room.kind === "dm" && others.length === 1 && others[0].kind === "agent" ? `agent:${others[0].id}` : `room:${room.roomId}`;
}

export interface RosterAgent {
  id: string;
  name?: string | null;
  status?: string | null;
  seeded?: boolean;
}

export type PinnedItem =
  | { key: PinKey; kind: "agent"; agentId: string; name: string; ring: AvatarRing | null; roomId: string | null }
  | { key: PinKey; kind: "room"; room: RoomSummary; name: string; others: RowParticipant[] }
  | { key: `session:${string}`; kind: "session"; session: LocalSession; name: string };

/**
 * What the strip draws: busy sessions first (when the switch is on, newest
 * first), then the pins in order. A pin whose target is gone (agent removed,
 * room left) is skipped, not deleted — the stored list only changes on the
 * reader's next edit.
 */
export function buildPinnedItems({
  pins, rooms, agents, sessions, showRunningSessions, viewerId,
}: {
  pins: readonly PinKey[];
  rooms: readonly RoomSummary[];
  agents: readonly RosterAgent[];
  sessions: readonly LocalSession[];
  showRunningSessions: boolean;
  viewerId: string | null | undefined;
}): PinnedItem[] {
  const running: PinnedItem[] = showRunningSessions
    ? sessions
        .filter((session) => session.status === "busy")
        .sort((a, b) => (b.lastEventAt ?? "").localeCompare(a.lastEventAt ?? ""))
        .map((session) => ({
          key: `session:${session.sessionId}` as const,
          kind: "session" as const,
          session,
          name: sessionToolName(session.provider),
        }))
    : [];
  const pinned = pins.flatMap((key): PinnedItem[] => {
    const id = key.slice(key.indexOf(":") + 1);
    if (key.startsWith("room:")) {
      const room = rooms.find((candidate) => candidate.roomId === id);
      if (!room) return [];
      const others = otherMembers(room, viewerId);
      return [{ key, kind: "room", room, name: roomDisplayName(room, others), others }];
    }
    const dm = rooms.find(
      (candidate) => candidate.kind === "dm" && otherMembers(candidate, viewerId).some((m) => m.kind === "agent" && m.id === id),
    );
    const agent = agents.find((candidate) => candidate.id === id);
    const name = agent?.name || dm?.members.find((m) => m.kind === "agent" && m.id === id)?.displayName;
    if (!name) return [];
    return [{ key, kind: "agent", agentId: id, name, ring: agentRingState(agent?.status), roomId: dm?.roomId ?? null }];
  });
  return [...running, ...pinned];
}
```

- [ ] **Step 4: Run the test**

Run: `cd apps/mobile && pnpm jest src/lib/__tests__/chats.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/lib/chats.ts apps/mobile/src/lib/__tests__/chats.test.ts
git commit -m "feat(mobile): phone-local pins, archived sessions and pin resolution

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Workspace roster hook (agents with status, people)

**Files:**
- Create: `apps/mobile/src/lib/workspaceRoster.ts`
- Modify: `apps/mobile/src/lib/teamsData.ts:242-246` (`TeamsAgent`)
- Test: `apps/mobile/src/lib/__tests__/workspaceRoster.test.tsx`

**Interfaces:**
- Consumes: `fetchAgents`, `fetchMembers`, `useTeamsConfig`, `TeamsAgent`, `CompanyMember` (`./teamsData`).
- Produces: `TeamsAgent` gains `status?: string | null; role?: string | null` (the BFF already returns them — web `useRosterEntries` reads the same `/teams/agents`); `useWorkspaceRoster(enabled: boolean): { agents: TeamsAgent[]; members: CompanyMember[] }`; `ROSTER_POLL_MS = 30_000`.
- Behaviour: while `enabled`, fetch agents + members immediately, then agents every `ROSTER_POLL_MS` (status drives the rings). A failed fetch resolves to `[]` in `teamsData`; an empty result never replaces a non-empty list (ponytail: a real roster of zero agents keeps the last list until remount — fine for a six-agent starter roster).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/lib/__tests__/workspaceRoster.test.tsx
import { act, renderHook } from "@testing-library/react-native";

const mockFetchAgents = jest.fn();
const mockFetchMembers = jest.fn();
const mockConfig = {};
jest.mock("../teamsData", () => ({
  useTeamsConfig: () => mockConfig,
  fetchAgents: (...a: unknown[]) => mockFetchAgents(...a),
  fetchMembers: (...a: unknown[]) => mockFetchMembers(...a),
}));

import { ROSTER_POLL_MS, useWorkspaceRoster } from "../workspaceRoster";

beforeEach(() => {
  jest.useFakeTimers();
  mockFetchAgents.mockReset();
  mockFetchMembers.mockReset();
  mockFetchMembers.mockResolvedValue([{ userId: "u1", name: "Leo" }]);
});
afterEach(() => jest.useRealTimers());

it("loads agents with status and members, then re-polls agents", async () => {
  mockFetchAgents.mockResolvedValueOnce([{ id: "ceo", name: "CEO", status: "idle", seeded: true }])
    .mockResolvedValueOnce([{ id: "ceo", name: "CEO", status: "running", seeded: true }]);
  const { result } = await renderHook(() => useWorkspaceRoster(true));
  await act(async () => {});
  expect(result.current.agents[0].status).toBe("idle");
  expect(result.current.members).toEqual([{ userId: "u1", name: "Leo" }]);
  await act(async () => { jest.advanceTimersByTime(ROSTER_POLL_MS); });
  expect(result.current.agents[0].status).toBe("running");
});

it("keeps the last roster when a poll comes back empty", async () => {
  mockFetchAgents.mockResolvedValueOnce([{ id: "ceo", name: "CEO" }]).mockResolvedValueOnce([]);
  const { result } = await renderHook(() => useWorkspaceRoster(true));
  await act(async () => {});
  await act(async () => { jest.advanceTimersByTime(ROSTER_POLL_MS); });
  expect(result.current.agents).toHaveLength(1);
});

it("does nothing while disabled", async () => {
  await renderHook(() => useWorkspaceRoster(false));
  expect(mockFetchAgents).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/lib/__tests__/workspaceRoster.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

In `teamsData.ts` extend the interface:

```ts
export interface TeamsAgent {
  id: string;
  name?: string | null;
  seeded?: boolean;
  /** Paperclip's agent status, verbatim (running / idle / paused / error …): the avatar ring. */
  status?: string | null;
  role?: string | null;
}
```

```ts
// apps/mobile/src/lib/workspaceRoster.ts
import { useEffect, useState } from "react";

import { fetchAgents, fetchMembers, useTeamsConfig, type CompanyMember, type TeamsAgent } from "./teamsData";

export const ROSTER_POLL_MS = 30_000;

/**
 * The workspace's agents (with Paperclip status, for rings and the starter
 * pins) and people (session owners' names). Agents re-poll while enabled;
 * people load once per enable.
 *
 * ponytail: an empty poll never replaces a non-empty list, so a genuine drop to
 * zero agents shows the last roster until remount.
 */
export function useWorkspaceRoster(enabled: boolean): { agents: TeamsAgent[]; members: CompanyMember[] } {
  const config = useTeamsConfig();
  const [agents, setAgents] = useState<TeamsAgent[]>([]);
  const [members, setMembers] = useState<CompanyMember[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const loadAgents = () =>
      void fetchAgents(config).then((list) => {
        if (alive && list.length > 0) setAgents(list);
      });
    loadAgents();
    void fetchMembers(config).then((list) => {
      if (alive) setMembers(list);
    });
    const timer = setInterval(loadAgents, ROSTER_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [enabled, config]);
  return { agents, members };
}
```

- [ ] **Step 4: Run the test**

Run: `cd apps/mobile && pnpm jest src/lib/__tests__/workspaceRoster.test.tsx && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/lib/workspaceRoster.ts apps/mobile/src/lib/teamsData.ts apps/mobile/src/lib/__tests__/workspaceRoster.test.tsx
git commit -m "feat(mobile): workspace roster hook with agent status

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: ChatRow — room and session rows with swipes and menus

**Files:**
- Create: `apps/mobile/src/components/chats/ChatRow.tsx`
- Test: `apps/mobile/src/components/chats/__tests__/ChatRow.test.tsx`

**Interfaces:**
- Consumes: `ListRow` (Task 4), `Avatar` (Task 3), `RowParticipant` (Task 5), `roomPreview`, `sessionPreview`, `sessionRingState`, `sessionLabel`, `roomDisplayName`, `AvatarRing` (Task 2 / shared), `relativeTime`, `hapticSelection`.
- Produces:
  ```ts
  export type ChatRowAction = "markRead" | "markUnread" | "mute" | "star" | "copyLink" | "pin" | "archive";
  export function RoomChatRow(props: {
    room: RoomSummary; others: readonly RowParticipant[]; viewerId: string | null | undefined;
    ring: AvatarRing | null;            // the DM agent's ring (null for channels/groups/humans)
    working: boolean;                   // DM agent is running → preview "working…" in link colour
    hasDraft: boolean; pinned: boolean; palette: ChatPalette;
    onAction: (action: ChatRowAction) => void;
  }): JSX.Element
  export function SessionChatRow(props: {
    session: LocalSession; ownerName: string; own: boolean; unread: boolean; palette: ChatPalette;
    onAction: (action: Extract<ChatRowAction, "markRead" | "archive">) => void;
  }): JSX.Element
  ```
- Room row behaviour:
  - Leading 52pt: channel → a 52pt tile (`palette.selected`, radius 12) with `#` in `fonts.sansSemiBold` 22pt `palette.fgSoft`; `dm` → `<Avatar name kind ring online sharing showPresence palette size={52} />` of `others[0]`; `group-dm` → the first two others as 36pt avatars overlapped (second offset `left: 16, top: 16`).
  - Title `roomDisplayName(room, others)`; `titleAccessory`: "Draft" (13pt, `palette.alert`) when `hasDraft`, "Shared" (13pt, `palette.fgMute`) when `room.shared`, and a `bell.slash` SF symbol (13pt, `palette.fgMute`, `accessibilityElementsHidden`) when `room.muted`.
  - Time `relativeTime(Date.parse(room.lastMessageAt ?? ""))`.
  - Preview: `working` → "working…" with `previewTone="live"`; else `roomPreview(room, viewerId)`.
  - `unread = room.unreadCount > 0 && !room.muted`.
  - Accessibility label: `${name}${unread ? `, ${room.unreadCount} unread` : ""}${room.muted ? ", muted" : ""}${hasDraft ? ", draft" : ""}${working ? ", working" : ""}`; actions: read/unread, mute/unmute, star/unstar, pin/unpin, copy link.
  - Wrapped in `ReanimatedSwipeable` with the exact open-row bookkeeping and swipe-action anatomy from today's `components/room/RoomRow.tsx:171-289` (module-level `openRow`, `overshootLeft/Right={false}`, `SwipeAction` glyph + label, `accessibilityElementsHidden`): leading swipe = Read/Unread (`envelope.open.fill` / `envelope.badge.fill`, fill `palette.link`); trailing swipe = Mute/Unmute (`bell.slash.fill` / `bell.fill`, fill `palette.fgSoft`); `childrenContainerStyle={{ backgroundColor: palette.surface }}`.
  - Inside it: `<Link href={`/(app)/room/${room.roomId}`} asChild><Link.Trigger><ListRow …/></Link.Trigger><Link.Preview /><Link.Menu>` with MenuActions in this order: Pin/Unpin (`pin` / `pin.slash`), Star/Unstar, Mute/Unmute, Mark unread (`envelope.badge`), Copy link (`link`).
- Session row behaviour:
  - Leading `<Avatar name={sessionLabel(session)} kind="agent" vendor={session.provider} ring={sessionRingState(session)} size={52} palette />`.
  - Title `sessionLabel(session)`; time `relativeTime(Date.parse(session.lastEventAt ?? ""))`.
  - Preview: busy → `${own ? "" : `${ownerName} · `}working…` with `previewTone="live"`; else `${own ? "" : `${ownerName}: `}${sessionPreview(session)}`.
  - `unread` prop draws the dot.
  - Swipes: leading "Read" (only when `unread`), trailing "Archive" (`archivebox.fill`, `palette.fgSoft`).
  - `Link href={`/(app)/session/${session.sessionId}`}` with Trigger + Menu (Mark read — only when unread —, Archive); no `Link.Preview` (the session screen has no read-only peek mode).
  - Accessibility label `${sessionLabel(session)}, ${sessionStatus[session.status]}${unread ? ", new output" : ""}${own ? "" : `, ${ownerName}`}`; actions markRead (when unread), archive.

- [ ] **Step 1: Write the failing test**

Mocks: reuse the `expo-router` `Link` mock and the `ReanimatedSwipeable` flattening mock exactly as written in `apps/mobile/src/app/(app)/(tabs)/__tests__/home.test.tsx` (the `jest.mock("expo-router", …)` factory's `Link` part and the `jest.mock("react-native-gesture-handler/ReanimatedSwipeable", …)` factory), then:

```tsx
// apps/mobile/src/components/chats/__tests__/ChatRow.test.tsx  (after the two mocks)
import { fireEvent, render, screen, within } from "@testing-library/react-native";
import type { LocalSession, RoomSummary } from "@lightbulb/shared";

import { chatPalette } from "../../../config/theme";
import { RoomChatRow, SessionChatRow } from "../ChatRow";

const p = chatPalette.light;
const dm: RoomSummary = {
  roomId: "dm1", name: null, kind: "dm", roomRevision: 1, lastMessageSeq: 4, lastMessageAt: "2026-09-26T09:12:00Z",
  unreadCount: 2, updatedAt: "x", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 2,
  lastMessage: { messageId: "m", seq: 4, authorId: "ceo", authorKind: "agent", authorName: "CEO", textPreview: "Prod deploy landed", hasAttachments: false, createdAt: "x", threadRootMessageId: null },
};
const ceo = { kind: "agent" as const, id: "ceo", displayName: "CEO" };

describe("RoomChatRow", () => {
  it("shows the counterpart, preview and unread dot, and opens the room", async () => {
    await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} working={false} hasDraft={false} pinned={false} palette={p} onAction={jest.fn()} />);
    expect(screen.getByText("CEO")).toBeTruthy();
    expect(screen.getByText("Prod deploy landed")).toBeTruthy();
    expect(screen.getByLabelText("Unread")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("CEO, 2 unread"));
    expect(mockNavigate).toHaveBeenCalledWith("/(app)/room/dm1");
  });

  it("says working in the preview while the DM agent runs", async () => {
    await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring="running" working hasDraft={false} pinned={false} palette={p} onAction={jest.fn()} />);
    expect(screen.getByText("working…")).toBeTruthy();
    expect(screen.getByLabelText("Working")).toBeTruthy();
  });

  it("hides the dot and shows a bell for a muted room", async () => {
    await render(<RoomChatRow room={{ ...dm, muted: true }} others={[ceo]} viewerId="me" ring={null} working={false} hasDraft={false} pinned={false} palette={p} onAction={jest.fn()} />);
    expect(screen.queryByLabelText("Unread")).toBeNull();
    expect(screen.getByLabelText("CEO, muted")).toBeTruthy();
  });

  it("offers Pin in the menu and Unpin once pinned", async () => {
    const onAction = jest.fn();
    const { rerender } = await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} working={false} hasDraft={false} pinned={false} palette={p} onAction={onAction} />);
    await fireEvent.press(within(screen.getByTestId("link-/(app)/room/dm1")).getByText("Pin"));
    expect(onAction).toHaveBeenCalledWith("pin");
    await rerender(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} working={false} hasDraft={false} pinned palette={p} onAction={onAction} />);
    expect(within(screen.getByTestId("link-/(app)/room/dm1")).getByText("Unpin")).toBeTruthy();
  });

  it("swipes to mark read and to mute", async () => {
    const onAction = jest.fn();
    await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} working={false} hasDraft={false} pinned={false} palette={p} onAction={onAction} />);
    await fireEvent.press(screen.getByLabelText("Mark read", { includeHiddenElements: true }));
    await fireEvent.press(screen.getByLabelText("Mute conversation", { includeHiddenElements: true }));
    expect(onAction.mock.calls).toEqual([["markRead"], ["mute"]]);
  });
});

describe("SessionChatRow", () => {
  const session: LocalSession = {
    sessionId: "s1", deviceId: "d", humanUserId: "leo", provider: "claude", generation: "g", status: "busy",
    providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
    latestEventSeq: 3, title: "lightbulb", lastEventAt: "2026-09-26T09:38:00Z",
  };

  it("draws the vendor mark, a live ring and the owner's working line", async () => {
    await render(<SessionChatRow session={session} ownerName="Leo" own={false} unread palette={p} onAction={jest.fn()} />);
    expect(screen.getByTestId("session-tool-claude")).toBeTruthy();
    expect(screen.getByLabelText("Working")).toBeTruthy();
    expect(screen.getByText("Leo · working…")).toBeTruthy();
  });

  it("opens the session and archives from the swipe", async () => {
    const onAction = jest.fn();
    await render(<SessionChatRow session={{ ...session, status: "idle" }} ownerName="Leo" own unread={false} palette={p} onAction={onAction} />);
    await fireEvent.press(screen.getByLabelText("Claude · lightbulb, Idle"));
    expect(mockNavigate).toHaveBeenCalledWith("/(app)/session/s1");
    await fireEvent.press(screen.getByLabelText("Archive session", { includeHiddenElements: true }));
    expect(onAction).toHaveBeenCalledWith("archive");
  });
});
```

(`mockNavigate` is the `jest.fn()` the copied `Link` mock calls; declare `const mockNavigate = jest.fn();` above the mocks and `beforeEach(() => mockNavigate.mockClear())`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/components/chats/__tests__/ChatRow.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `ChatRow.tsx`** to the Interfaces block. Swipe button labels (VoiceOver-hidden buttons, but the tests find them by `accessibilityLabel`): "Mark read" / "Mark unread", "Mute conversation" / "Unmute conversation", "Archive session". Menu action titles: "Pin" / "Unpin", "Star" / "Unstar", "Mute" / "Unmute", "Mark unread", "Copy link", "Mark read", "Archive".

- [ ] **Step 4: Run the test**

Run: `cd apps/mobile && pnpm jest src/components/chats/__tests__/ChatRow.test.tsx && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/chats
git commit -m "feat(mobile): chat rows for rooms and sessions

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: PinnedStrip — pins, unpin, reorder mode

**Files:**
- Create: `apps/mobile/src/components/chats/PinnedStrip.tsx`
- Test: `apps/mobile/src/components/chats/__tests__/PinnedStrip.test.tsx`

**Interfaces:**
- Consumes: `PinnedItem`, `PinKey` (Task 5), `Avatar` (Task 3), `ChatPalette`, `sessionRingState`.
- Produces:
  ```ts
  export function PinnedStrip(props: {
    items: readonly PinnedItem[];
    palette: ChatPalette;
    reordering: boolean;
    onOpen: (item: PinnedItem) => void;
    onUnpin: (key: PinKey) => void;
    onMove: (key: PinKey, delta: -1 | 1) => void;
    onReorder: (on: boolean) => void;
  }): JSX.Element | null
  ```
- Behaviour:
  - `items.length === 0` → `null`.
  - A horizontal `ScrollView` (`showsHorizontalScrollIndicator={false}`, `contentContainerStyle` padding 16, gap 12), `testID="pinned-strip"`.
  - Each item is a 72pt-wide column: `Avatar size={56}` (agent → `kind="agent"`, `ring`; room → channel `#` tile as in ChatRow or the DM counterpart's avatar; session → `vendor={session.provider}`, `ring={sessionRingState(session)}`), then the name (12pt `fonts.sans`, `palette.fgSoft`, 1 line, centred).
  - Tap → `onOpen(item)` (disabled while reordering). `accessibilityLabel`: `${name}${ring ? `, ${RING word}` : ""}`, `accessibilityRole="button"`.
  - Long-press on an agent/room item (not a session) → `ActionSheetIOS.showActionSheetWithOptions({ options: ["Unpin", "Reorder pins", "Cancel"], destructiveButtonIndex: 0, cancelButtonIndex: 2, title: name }, (i) => i === 0 ? onUnpin(key) : i === 1 ? onReorder(true) : undefined)`. `accessibilityActions` mirror both ("unpin", "reorder").
  - Reordering: sessions are hidden from the strip; each pin shows two 28pt chevron buttons (`chevron.left` / `chevron.right`, `hitSlop={8}`) in place of its name, labelled `Move ${name} left` / `Move ${name} right`, the first item's left and the last item's right disabled; a trailing "Done" text button (`fonts.sansSemiBold` 15, `palette.link`, 44pt) calls `onReorder(false)`.
  - Rationale (spec conflict): the spec gives long-press on a pin both "Unpin" and "reorder mode"; one menu offers both, and move buttons replace drag so reordering works with VoiceOver and needs no gesture code.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/chats/__tests__/PinnedStrip.test.tsx
import { fireEvent, render, screen } from "@testing-library/react-native";
import { ActionSheetIOS } from "react-native";
import type { LocalSession, RoomSummary } from "@lightbulb/shared";

import { chatPalette } from "../../../config/theme";
import type { PinnedItem } from "../../../lib/chats";
import { PinnedStrip } from "../PinnedStrip";

const p = chatPalette.light;
const general = { roomId: "gen", name: "general", kind: "channel", members: [] } as unknown as RoomSummary;
const busy = { sessionId: "s1", provider: "codex", status: "busy" } as unknown as LocalSession;
const items: PinnedItem[] = [
  { key: "session:s1", kind: "session", session: busy, name: "Codex" },
  { key: "agent:ceo", kind: "agent", agentId: "ceo", name: "CEO", ring: "running", roomId: "dm1" },
  { key: "room:gen", kind: "room", room: general, name: "general", others: [] },
];
const handlers = () => ({ onOpen: jest.fn(), onUnpin: jest.fn(), onMove: jest.fn(), onReorder: jest.fn() });

it("renders nothing without items", async () => {
  await render(<PinnedStrip items={[]} palette={p} reordering={false} {...handlers()} />);
  expect(screen.queryByTestId("pinned-strip")).toBeNull();
});

it("opens a pin on tap and labels its ring", async () => {
  const h = handlers();
  await render(<PinnedStrip items={items} palette={p} reordering={false} {...h} />);
  await fireEvent.press(screen.getByLabelText("CEO, working"));
  expect(h.onOpen).toHaveBeenCalledWith(items[1]);
  expect(screen.getByTestId("session-tool-codex")).toBeTruthy();
});

it("offers Unpin and Reorder on long press", async () => {
  const h = handlers();
  const sheet = jest.spyOn(ActionSheetIOS, "showActionSheetWithOptions").mockImplementation((_o, cb) => cb(0));
  await render(<PinnedStrip items={items} palette={p} reordering={false} {...h} />);
  await fireEvent(screen.getByLabelText("general"), "longPress");
  expect(h.onUnpin).toHaveBeenCalledWith("room:gen");
  sheet.mockImplementation((_o, cb) => cb(1));
  await fireEvent(screen.getByLabelText("general"), "longPress");
  expect(h.onReorder).toHaveBeenCalledWith(true);
  sheet.mockRestore();
});

it("moves pins with buttons in reorder mode and hides sessions", async () => {
  const h = handlers();
  await render(<PinnedStrip items={items} palette={p} reordering {...h} />);
  expect(screen.queryByTestId("session-tool-codex")).toBeNull();
  await fireEvent.press(screen.getByLabelText("Move general left"));
  expect(h.onMove).toHaveBeenCalledWith("room:gen", -1);
  expect(screen.getByLabelText("Move CEO left").props.accessibilityState).toMatchObject({ disabled: true });
  await fireEvent.press(screen.getByText("Done"));
  expect(h.onReorder).toHaveBeenCalledWith(false);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/components/chats/__tests__/PinnedStrip.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `PinnedStrip.tsx`** to the Interfaces block (ring words for the label: running "working", attention "needs attention", paused "paused").

- [ ] **Step 4: Run the test**

Run: `cd apps/mobile && pnpm jest src/components/chats/__tests__/PinnedStrip.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/chats/PinnedStrip.tsx apps/mobile/src/components/chats/__tests__/PinnedStrip.test.tsx
git commit -m "feat(mobile): pinned strip with unpin and reorder mode

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: The Chats tab screen

**Files:**
- Create: `apps/mobile/src/lib/tabHeader.ts`
- Create: `apps/mobile/src/app/(app)/(tabs)/chats.tsx`
- Test: `apps/mobile/src/app/(app)/(tabs)/__tests__/chats.test.tsx` (created with `git mv` from `home.test.tsx` in Step 1)

**Interfaces:**
- Consumes: everything from Tasks 1–8; `useMobileRooms`, `useLiveEvents`, `useWorkspaceSessions`, `sessionUnread`, `markSessionsSeenOnDevice`, `roomIdsWithDrafts`, `NewMessageSheet`, `LogoMark`, `roomUrl`.
- Produces:
  ```ts
  // lib/tabHeader.ts
  /** Writes `options` into the shared (app) Stack header while THIS tab is the selected one;
   *  on deselect runs `onLeave` and resets every key it set to undefined. */
  export function useTabHeader(options: TabHeaderOptions, onLeave?: () => void): void
  // (tabs)/chats.tsx
  export default function Chats(): JSX.Element
  ```

`tabHeader.ts` (full code — it is the extracted form of today's `home.tsx` header effect, keyed on tab SELECTION, not focus, for the reason documented there):

```ts
import { useNavigation, useRoute, type NativeStackNavigationProp } from "expo-router";
import { useNavigationState, type ParamListBase } from "expo-router/react-navigation";
import { useLayoutEffect, useRef } from "react";

/** The (app) Stack's own options type, derived so no extra package import is needed. */
export type TabHeaderOptions = Parameters<NativeStackNavigationProp<ParamListBase>["setOptions"]>[0];

/**
 * The four tab roots are ONE screen of the (app) Stack and share its header, so
 * a tab writes its own header items there — keyed on the tab bar's SELECTION,
 * not on focus: a screen pushed over the tabs blurs the tab too, and stripping
 * the header then would pull the search bar out of the outgoing nav bar
 * mid-push. Options are read once per selection (through a ref), so every
 * closure in them must only call stable setters.
 */
export function useTabHeader(options: TabHeaderOptions, onLeave?: () => void): void {
  const header = useNavigation<NativeStackNavigationProp<ParamListBase>>("/(app)");
  const { key } = useRoute();
  const selected = useNavigationState((state) => state.routes[state.index]?.key === key);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const leaveRef = useRef(onLeave);
  leaveRef.current = onLeave;
  useLayoutEffect(() => {
    if (!selected) return;
    const set = optionsRef.current;
    header.setOptions(set);
    return () => {
      leaveRef.current?.();
      header.setOptions(Object.fromEntries(Object.keys(set).map((name) => [name, undefined])));
    };
  }, [header, selected]);
}
```

Chats screen behaviour (`(tabs)/chats.tsx`):
- `const palette = useChatPalette(); const focused = useIsFocused(); const { userId, orgId } = useAuth();` `const scope = orgId ?? userId ?? "none"` (workspace key for prefs).
- Data: `rooms = useMobileRooms({ autoSelect: false, subscribeToRoom: false })`; `{ sessions } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused })`; `{ agents, members } = useWorkspaceRoster(focused)`; `const { messaging } = useLiveEvents()`.
- Prefs state, re-read on every focus via `useFocusEffect`: `storedPins = readPins(scope)`, `showRunning = readShowRunningSessions()`, `archived = readArchivedSessions(scope)`, `drafts = roomIdsWithDrafts(draftScope)` (same `draftScope` rule as `home.tsx`), and `seen = markSessionsSeenOnDevice(sessions, null)` recomputed when `sessions` changes while focused.
- `seeded = agents.filter((a) => a.seeded).map((a) => a.id)`; `pins = resolvePins(storedPins, seeded)`.
- `editPins(next: PinKey[])` → `writePins(scope, next)` then `setStoredPins(next)`.
- Header via `useTabHeader({ headerLargeTitle: true, headerLargeTitleShadowVisible: false, headerLargeTitleStyle: { fontFamily: fonts.loraMedium }, headerSearchBarOptions: { ref: searchBar, placeholder: "Search", autoCapitalize: "none", placement: "stacked", hideWhenScrolling: false, onChangeText: (e) => setQuery(e.nativeEvent.text), onCancelButtonPress: () => setQuery("") }, headerRight: () => <ComposeButton onPress={() => setPickerOpen(true)} /> }, () => { searchBar.current?.cancelSearch(); setQuery(""); })`. `ComposeButton` is today's `home.tsx` `ComposeButton` moved verbatim with `tintColor={palette.fgSoft}`.
- `entries = mergeChats(rooms.rooms, sessions, { archivedSessions: archived })`, filtered by `query` (trimmed, leading `#` stripped, case-insensitive) against: room → `roomDisplayName(room, otherMembers(room, userId))`; session → `sessionLabel(session)` and the owner name.
- Owner name: `member.name || member.email || "Workspace member"` from `members` by `session.humanUserId`, else `memberName(room members, id)` (existing `lib/localSessions.memberName`); the viewer's own sessions are "You".
- `rosterById = new Map(agents.map((a) => [a.id, a]))`. For a room row: `others = otherMembers(room, userId)` decorated `sharing: sessions.some((s) => s.humanUserId === m.id && !sessionArchived(s))` for humans; `dmAgent = room.kind === "dm" && others[0]?.kind === "agent" ? others[0] : null`; `ring = dmAgent ? agentRingState(rosterById.get(dmAgent.id)?.status) : null`; `working = ring === "running"`; `pinned = pins.includes(pinKeyForRoom(room, others))`.
- Strip: `buildPinnedItems({ pins, rooms: rooms.rooms, agents, sessions, showRunningSessions: showRunning, viewerId: userId })`, rendered as the list's `ListHeaderComponent` ABOVE the error card, and hidden while `query.trim() !== ""`. `onOpen`: session → `router.push(`/(app)/session/${id}`)`; room → `router.push(`/(app)/room/${roomId}`)`; agent → `roomId ? push(room) : void rooms.selectDm({ kind: "agent", id: agentId }, { activate: false }).then((id) => id && router.push(`/(app)/room/${id}`)).catch(() => {})`. `onUnpin(key)` → `editPins(togglePin(pins, key))`; `onMove` → `editPins(movePin(pins, key, delta))`; `reordering` is local state.
- Row actions (room): `markRead` → `messaging.markRead(roomId, { lastReadSeq: room.lastMessageSeq }).then(rooms.refreshRooms)`; `markUnread` → `rooms.markUnread(roomId)`; `mute`/`star` → `rooms.setRoomPrefs(roomId, { muted: !room.muted })` / `{ starred: !room.starred }` with today's failure copy ("The notification setting could not be saved." / "The star could not be saved.") shown above the rows; `copyLink` → `Clipboard.setStringAsync(roomUrl(roomId))`; `pin` → `editPins(togglePin(pins, pinKeyForRoom(room, others)))`. Session: `markRead` → `markSessionsSeenOnDevice([session], session.sessionId)` then refresh `seen`; `archive` → `archiveSession(scope, session)` then refresh `archived`.
- List: `FlatList` `testID="chat-list"`, `contentInsetAdjustmentBehavior="automatic"`, `automaticallyAdjustKeyboardInsets`, page `backgroundColor: palette.surface`, pull-to-refresh → `Promise.all([rooms.refreshRooms()])`, empty: skeleton while `rooms.roomsLoading`, else `LogoMark` + "No conversations match that." / "No conversations yet." (`palette.fgMute`), rooms error card as today (colours mapped per Global Constraints). `NewMessageSheet` as today.

- [ ] **Step 1: Move the test scaffolding**

```bash
git mv 'apps/mobile/src/app/(app)/(tabs)/__tests__/home.test.tsx' 'apps/mobile/src/app/(app)/(tabs)/__tests__/chats.test.tsx'
```

In `chats.test.tsx`: keep every `jest.mock` factory and fixture above `import Dms from "../dms";` unchanged except: `useAuth: () => ({ userId: "user-a", orgId: mockOrgId })` with `let mockOrgId = "org-0"; let mockOrgSeq = 0;` declared above the mocks (the pins store is module state shared by every test in the file, so each test gets its own workspace); change `mockSelectedTab = "tab-home"` defaults and `useRoute: () => ({ key: "tab-home" })` to `"tab-chats"`; give `mockSummary` rooms a `lastMessage` where a test reads the preview; change `mockResourceSource.client.listWorkspaceSessions` to `jest.fn(async () => ({ sessions: mockSessions }))` with `let mockSessions: LocalSession[] = [];` declared above it; add:

```tsx
let mockAgents: { id: string; name: string; status?: string; seeded?: boolean }[] = [];
jest.mock("../../../../lib/workspaceRoster", () => ({
  useWorkspaceRoster: () => ({ agents: mockAgents, members: [{ userId: "user-b", name: "Leo" }] }),
}));
```

Give the `r2` DM its agent member directly on the summary (`members: [mockMember({ role: "owner" }), mockMember({ kind: "agent", id: "agent-1", displayName: "Friday" })]`) since Chats reads `room.members`. Delete everything from `import Dms from "../dms";` to the end and write:

```tsx
import { ActionSheetIOS } from "react-native";

import { readPins, writePins, writeShowRunningSessions } from "../../../../lib/chats";
import Chats from "../chats";

const ChatsTab = () => (
  <>
    <MockHeader />
    <Chats />
  </>
);
const busySession: LocalSession = {
  sessionId: "sess-1", deviceId: "mac", humanUserId: "user-b", provider: "claude", generation: "g", status: "busy",
  providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 4, title: "lightbulb", lastEventAt: "2026-09-11T11:30:00Z",
};

describe("Chats", () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockNavigate.mockClear();
    mockRefreshRooms.mockClear();
    mockMarkRead.mockClear();
    mockSetRoomPrefs.mockClear();
    mockHeaderOptions = {};
    mockSelectedTab = "tab-chats";
    mockOrgId = `org-${++mockOrgSeq}`;
    mockFocused = true;
    mockRoomsState = {};
    mockSessions = [];
    mockAgents = [
      { id: "agent-1", name: "Friday", status: "running", seeded: true },
      { id: "agent-2", name: "Carol", status: "idle", seeded: true },
      { id: "agent-9", name: "Hired", status: "idle", seeded: false },
    ];
    writeShowRunningSessions(true);
  });

  it("merges rooms and live sessions into one list, newest first", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    await waitFor(() => expect(screen.getAllByLabelText(/^Claude · lightbulb/).length).toBeGreaterThan(0));
    const order = screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID);
    expect(order.slice(0, 3)).toEqual(["chat-room-r3", "chat-session-sess-1", "chat-room-r2"]);
  });

  it("defaults the strip to the starter agents in roster order and writes nothing", async () => {
    await render(<ChatsTab />);
    const strip = await screen.findByTestId("pinned-strip");
    expect(within(strip).getByLabelText("Friday, working")).toBeTruthy();
    expect(within(strip).getByLabelText("Carol")).toBeTruthy();
    expect(within(strip).queryByLabelText("Hired")).toBeNull();
    expect(readPins(mockOrgId)).toBeNull();
  });

  it("shows a running session first in the strip only while the switch is on", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    const strip = await screen.findByTestId("pinned-strip");
    expect(within(strip).getAllByRole("button")[0].props.accessibilityLabel).toBe("Claude, working");
    writeShowRunningSessions(false);
    await render(<ChatsTab />);
    expect(within(await screen.findByTestId("pinned-strip")).queryByLabelText("Claude, working")).toBeNull();
  });

  it("pins a channel from its row menu and persists the whole list", async () => {
    await render(<ChatsTab />);
    await fireEvent.press(within(await screen.findByTestId("link-/(app)/room/r1")).getByText("Pin"));
    expect(readPins(mockOrgId)).toEqual(["agent:agent-1", "agent:agent-2", "room:r1"]);
    expect(within(screen.getByTestId("pinned-strip")).getByLabelText("launch")).toBeTruthy();
  });

  it("unpins from the strip", async () => {
    writePins(mockOrgId, ["agent:agent-2"]);
    jest.spyOn(ActionSheetIOS, "showActionSheetWithOptions").mockImplementation((_o, cb) => cb(0));
    await render(<ChatsTab />);
    await fireEvent(within(await screen.findByTestId("pinned-strip")).getByLabelText("Carol"), "longPress");
    expect(readPins(mockOrgId)).toEqual([]);
  });

  it("hides the strip and filters rows while searching", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    await fireEvent.changeText(await screen.findByPlaceholderText("Search"), "#laun");
    await waitFor(() => expect(screen.queryByTestId("pinned-strip")).toBeNull());
    expect(screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID)).toEqual(["chat-room-r1"]);
  });

  it("opens a session row on its own screen", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    await fireEvent.press(await screen.findByLabelText("Claude · lightbulb, Working, Leo"));
    expect(mockNavigate).toHaveBeenCalledWith("/(app)/session/sess-1");
  });

  it("archives a session off the list", async () => {
    mockSessions = [{ ...busySession, status: "idle" }];
    await render(<ChatsTab />);
    await fireEvent.press(await screen.findByLabelText("Archive session", { includeHiddenElements: true }));
    await waitFor(() => expect(screen.queryByTestId("chat-session-sess-1")).toBeNull());
  });

  it("marks a room read from its swipe", async () => {
    await render(<ChatsTab />);
    await fireEvent.press(within(await screen.findByTestId("chat-room-r2")).getByLabelText("Mark read", { includeHiddenElements: true }));
    await waitFor(() => expect(mockMarkRead).toHaveBeenCalledWith("r2", { lastReadSeq: 7 }));
  });

  it("puts the compose button and a large title in the shared header", async () => {
    await render(<ChatsTab />);
    await waitFor(() => expect(mockSetOptions).toHaveBeenCalledWith(expect.objectContaining({ headerLargeTitle: true })));
    await fireEvent.press(screen.getByLabelText("New message"));
    expect(screen.getByText("MOCK_NEW_MESSAGE_SHEET")).toBeTruthy();
  });

  it("defaults arrive with the roster", async () => {
    mockAgents = [];
    const { rerender } = await render(<ChatsTab />);
    expect(screen.queryByTestId("pinned-strip")).toBeNull();
    mockAgents = [{ id: "agent-1", name: "Friday", status: "idle", seeded: true }];
    await rerender(<ChatsTab />);
    expect(await screen.findByTestId("pinned-strip")).toBeTruthy();
  });
});
```

`ChatRow` rows must be wrapped in a `View` with `testID={`chat-${entry.kind}-${id}`}` (the strip + list tests use it).

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__/chats.test.tsx'`
Expected: FAIL — `../chats` not found.

- [ ] **Step 3: Implement** `lib/tabHeader.ts` (code above) and `(tabs)/chats.tsx` to the behaviour list. Do not delete `home.tsx` yet (Task 10 does, with the tab layout).

- [ ] **Step 4: Run the test**

Run: `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__/chats.test.tsx' && pnpm typecheck && pnpm lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/lib/tabHeader.ts 'apps/mobile/src/app/(app)/(tabs)/chats.tsx' 'apps/mobile/src/app/(app)/(tabs)/__tests__/chats.test.tsx'
git commit -m "feat(mobile): Chats tab — pinned strip over one merged list

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Tabs are Chats · Activity · Teams · Settings; retire Home, DMs, More and /sessions

**Files:**
- Modify: `apps/mobile/src/app/(app)/(tabs)/_layout.tsx`
- Move: `apps/mobile/src/app/(app)/teams/index.tsx` → `apps/mobile/src/app/(app)/(tabs)/teams.tsx`
- Move: `apps/mobile/src/app/(app)/settings.tsx` → `apps/mobile/src/app/(app)/(tabs)/settings.tsx`
- Move: `apps/mobile/src/app/(app)/__tests__/settings.test.tsx` → `apps/mobile/src/app/(app)/(tabs)/__tests__/settings.test.tsx`
- Delete: `(tabs)/home.tsx`, `(tabs)/dms.tsx`, `(tabs)/more.tsx`, `(tabs)/__tests__/more.test.tsx`, `(app)/sessions.tsx`, `(app)/__tests__/sessions.test.tsx`, `components/room/RoomRow.tsx`, `components/room/__tests__/RoomRow.test.tsx`
- Modify: `apps/mobile/src/app/(app)/_layout.tsx` (Stack screens, `TAB_TITLES`, comments), `apps/mobile/src/app/index.tsx:6`, `apps/mobile/src/app/(auth)/_layout.tsx:9`, `apps/mobile/src/app/(app)/workspaces.tsx:7`, `apps/mobile/src/app/(app)/agent/[agentId].tsx:27`, `apps/mobile/src/app/(app)/control/listing/[id].tsx:111`, `apps/mobile/src/lib/activity.ts:37`
- Modify tests: `(tabs)/__tests__/layout.test.tsx`, `(app)/__tests__/layout.test.tsx`, `agent/__tests__/agentRedirect.test.tsx`
- Create: `apps/mobile/src/__tests__/removedRoutes.test.ts`

**Interfaces:**
- Consumes: `useTabHeader` (Task 9).
- Produces: routes `/(app)/(tabs)/chats|activity|teams|settings`; `TAB_TITLES = { chats: "Chats", activity: "Activity", teams: "Teams", settings: "Settings" }` with fallback `"chats"`.

- [ ] **Step 1: Write the failing tests**

`(tabs)/__tests__/layout.test.tsx` — replace the order test and the Home-badge test:

```tsx
it("registers exactly Chats · Activity · Teams · Settings", async () => {
  await render(<TabsLayout />);
  expect(screen.getAllByTestId(/^tab-/).map((node) => node.props.testID)).toEqual([
    "tab-chats", "tab-activity", "tab-teams", "tab-settings",
  ]);
});

it("hides the Chats badge at zero and shows the count above it", async () => {
  await render(<TabsLayout />);
  expect(screen.queryByTestId("tab-badge")).toBeNull();
  mockUnread = 7;
  await render(<TabsLayout />);
  expect(within(screen.getByTestId("tab-chats")).getByTestId("tab-badge")).toHaveTextContent("7");
});
```

(add `within` to the `@testing-library/react-native` import; keep the Activity-badge and app-icon-badge tests as they are.)

`(app)/__tests__/layout.test.tsx`: in "owns both room screens…" iterate `["chats", "activity", "teams", "settings", "room"]`; replace "owns the Teams, Control, Settings…" with:

```tsx
it("owns Teams' detail screens, Control and the legacy agent route; Teams and Settings are tabs now", async () => {
  await render(<AppLayout />);
  expect(names()).toEqual(expect.arrayContaining(["teams/issue/[id]", "teams/run/[runId]", "control/index", "control/listing/[id]", "agent/[agentId]", "session/[sessionId]"]));
  for (const gone of ["teams/index", "settings", "sessions"]) expect(names()).not.toContain(gone);
});
```

and in "titles the tabs header…" expect `"Chats"` first, then `mockFocusedTab = "teams"` → `"Teams"`.

`agentRedirect.test.tsx:41`: expect `"/(app)/(tabs)/chats"`.

```ts
// apps/mobile/src/__tests__/removedRoutes.test.ts
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const SRC = join(__dirname, "..");
const REMOVED = [/\(tabs\)\/home\b/, /\(tabs\)\/dms\b/, /\(tabs\)\/more\b/, /\/\(app\)\/sessions\b/, /"\/\(app\)\/teams"/, /"\/\(app\)\/settings"/];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "__tests__" ? [] : sources(path);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

it("links no route that the redesign removed", () => {
  const hits = sources(SRC).flatMap((file) =>
    REMOVED.filter((pattern) => pattern.test(readFileSync(file, "utf8"))).map((pattern) => `${file}: ${pattern}`),
  );
  expect(hits).toEqual([]);
});

it("keeps sessions reachable from Chats", () => {
  expect(readFileSync(join(SRC, "app/(app)/(tabs)/chats.tsx"), "utf8")).toContain("/(app)/session/");
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__/layout.test.tsx' 'src/app/\(app\)/__tests__/layout.test.tsx' src/__tests__/removedRoutes.test.ts 'src/app/\(app\)/agent/__tests__/agentRedirect.test.tsx'`
Expected: FAIL.

- [ ] **Step 3: Implement**

```bash
cd apps/mobile/src/app
git mv '(app)/teams/index.tsx' '(app)/(tabs)/teams.tsx'
git mv '(app)/settings.tsx' '(app)/(tabs)/settings.tsx'
git mv '(app)/__tests__/settings.test.tsx' '(app)/(tabs)/__tests__/settings.test.tsx'
git rm '(app)/(tabs)/home.tsx' '(app)/(tabs)/dms.tsx' '(app)/(tabs)/more.tsx' '(app)/(tabs)/__tests__/more.test.tsx' '(app)/sessions.tsx' '(app)/__tests__/sessions.test.tsx' ../components/room/RoomRow.tsx ../components/room/__tests__/RoomRow.test.tsx
```

- Relative imports: `teams.tsx` sits at the same depth as `teams/index.tsx` (`../../../`) — unchanged. `settings.tsx` moves one level deeper: every `../../` becomes `../../../`. In the moved `settings.test.tsx`, `jest.mock("../../../lib/pushDevice"…)` becomes `"../../../../lib/pushDevice"`.
- `(tabs)/_layout.tsx`: triggers in order `chats` (icon `{ default: "message", selected: "message.fill" }`, label "Chats", the unread badge moved here), `activity` (unchanged), `teams` (`{ default: "person.2", selected: "person.2.fill" }`, "Teams"), `settings` (`{ default: "gearshape", selected: "gearshape.fill" }`, "Settings"). Rewrite the doc comment: "Chats · Activity · Teams · Settings (spec 2026-09-26): Home and DMs merged into Chats, More folded into Settings."
- `teams.tsx`: delete the `<Stack.Screen options={{ headerRight … }} />` block and replace it with `useTabHeader({ headerRight: () => (<the same Pressable "New task" button>) })` (drop the now-unused `Stack` import).
- `(app)/_layout.tsx`: `TAB_TITLES` per Interfaces with `?? "chats"` / `?? "Chats"` fallbacks; remove `Stack.Screen` entries `teams/index`, `sessions`, `settings`; update the header comments that name "Home and DMs" to "Chats".
- Redirects: `index.tsx`, `(auth)/_layout.tsx`, `workspaces.tsx` (`dismissTo`), `agent/[agentId].tsx` (`replace`) → `"/(app)/(tabs)/chats"`; `control/listing/[id].tsx:111` → `{ text: "Open Chats", onPress: () => router.navigate("/(app)/(tabs)/chats") }`.
- `lib/activity.ts:37`: `return ["/(app)/(tabs)/teams"];`.

- [ ] **Step 4: Run the tests**

Run: `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__' 'src/app/\(app\)/__tests__/layout.test.tsx' src/__tests__/removedRoutes.test.ts 'src/app/\(app\)/agent/__tests__/agentRedirect.test.tsx' src/components/push src/lib/__tests__ && pnpm typecheck && pnpm lint`
Expected: PASS (typed routes regenerate on typecheck; a stale `.expo/types` can be refreshed with `npx expo customize tsconfig.json` only if typecheck names a removed route).

- [ ] **Step 5: Commit**

```bash
git add -A apps/mobile/src
git commit -m "feat(mobile): tabs are Chats, Activity, Teams, Settings; retire Home, DMs, More, /sessions

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Settings absorbs More's rows and the running-sessions switch

**Files:**
- Modify: `apps/mobile/src/app/(app)/(tabs)/settings.tsx` (the `return (<GroupedList>…)` block)
- Modify: `apps/mobile/jest-setup.ts` (the `@expo/ui/swift-ui` mock)
- Test: `apps/mobile/src/app/(app)/(tabs)/__tests__/settings.test.tsx`

**Interfaces:**
- Consumes: `readShowRunningSessions`, `writeShowRunningSessions` (Task 5); `Toggle` from `@expo/ui/swift-ui`.
- Produces: Settings sections, in order: profile (+ Switch workspace), **Chats** (Toggle "Show running sessions in pinned", footer "Live Claude Code and Codex sessions appear at the start of your pinned chats while they run."), **Workspace** (`NavigationRow`s: Control `slider.horizontal.3` → `/(app)/control`; Integrations `puzzlepiece.extension` → `/(app)/control/integrations`; Marketplace `bag` → `/(app)/control/marketplace`), then the existing billing sections (incl. Plans), Legal, Help, Sign out, Delete account.

- [ ] **Step 1: Verify the `@expo/ui` switch API** (installed SDK 57):

Run: `ls node_modules/.pnpm | grep '^@expo+ui' && grep -rn "isOn\|onIsOnChange\|value\b\|onValueChange" node_modules/.pnpm/@expo+ui@*/node_modules/@expo/ui/build/swift-ui/Toggle/index.d.ts`
Expected: `Toggle` with `isOn` / `onIsOnChange` / `label`. If the SDK instead exports `Switch` with `value` / `onValueChange`, use those exact names in Steps 2–4 (the mock must mirror whatever the real component takes).

- [ ] **Step 2: Add the mock and write the failing test**

In `jest-setup.ts`, inside the `@expo/ui/swift-ui` mock's returned object, add:

```ts
    Toggle: (props: { label?: string; isOn?: boolean; onIsOnChange?: (on: boolean) => void }) =>
      react.createElement(
        Pressable,
        {
          accessibilityRole: "switch",
          accessibilityLabel: props.label,
          accessibilityState: { checked: !!props.isOn },
          onPress: () => props.onIsOnChange?.(!props.isOn),
        },
        text(props.label),
      ),
```

Append to `settings.test.tsx`:

```tsx
import { readShowRunningSessions, writeShowRunningSessions } from "../../../../lib/chats";

describe("Settings absorbs More", () => {
  beforeEach(() => {
    mockAccountFixture = { is_subscribed: true, subscription_status: "trial_local", trial_end: null, provider_choice: null, byo_provider: null };
    mockPush.mockClear();
    writeShowRunningSessions(true);
  });

  it("toggles running sessions in pinned, default on", async () => {
    await render(<Settings />);
    const toggle = await screen.findByLabelText("Show running sessions in pinned");
    expect(toggle.props.accessibilityState).toMatchObject({ checked: true });
    await fireEvent.press(toggle);
    expect(readShowRunningSessions()).toBe(false);
    expect(screen.getByLabelText("Show running sessions in pinned").props.accessibilityState).toMatchObject({ checked: false });
  });

  it.each([
    ["Control", "/(app)/control"],
    ["Integrations", "/(app)/control/integrations"],
    ["Marketplace", "/(app)/control/marketplace"],
  ])("links %s", async (label, route) => {
    await render(<Settings />);
    await fireEvent.press(await screen.findByLabelText(label));
    expect(mockPush).toHaveBeenCalledWith(route);
  });

  it("no longer links Teams or Shared sessions (Teams is a tab; sessions live in Chats)", async () => {
    await render(<Settings />);
    await screen.findByLabelText("Control");
    expect(screen.queryByText("Shared sessions")).toBeNull();
    expect(screen.queryByText("Teams")).toBeNull();
  });
});
```

Run: `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__/settings.test.tsx'`
Expected: FAIL — no toggle, no Control row.

- [ ] **Step 3: Implement** — in `settings.tsx` add `const [showRunning, setShowRunning] = useState(readShowRunningSessions);` and insert after the profile `Section`:

```tsx
      <Section
        title="Chats"
        footer={<Text>Live Claude Code and Codex sessions appear at the start of your pinned chats while they run.</Text>}
      >
        <Toggle
          label="Show running sessions in pinned"
          isOn={showRunning}
          onIsOnChange={(on) => {
            writeShowRunningSessions(on);
            setShowRunning(on);
          }}
        />
      </Section>

      <Section title="Workspace">
        <NavigationRow title="Control" icon="slider.horizontal.3" onPress={() => router.push("/(app)/control")} />
        <NavigationRow title="Integrations" icon="puzzlepiece.extension" onPress={() => router.push("/(app)/control/integrations")} />
        <NavigationRow title="Marketplace" icon="bag" onPress={() => router.push("/(app)/control/marketplace")} />
      </Section>
```

(add `Toggle` to the `@expo/ui/swift-ui` import and the two `lib/chats` imports.)

- [ ] **Step 4: Run the tests**

Run: `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__/settings.test.tsx' && pnpm typecheck`
Expected: PASS (all pre-existing Settings tests included).

- [ ] **Step 5: Commit**

```bash
git add 'apps/mobile/src/app/(app)/(tabs)/settings.tsx' 'apps/mobile/src/app/(app)/(tabs)/__tests__/settings.test.tsx' apps/mobile/jest-setup.ts
git commit -m "feat(mobile): Settings takes More's rows and the running-sessions switch

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Verification, fingerprint and simulator acceptance

**Files:** none changed (evidence goes in the PR description).

- [ ] **Step 1: Targeted suites, typecheck, lint**

Run:
```bash
cd packages/shared && pnpm test src/messaging/chatList.test.ts && pnpm typecheck
cd ../../apps/mobile && pnpm jest src/config src/components/room src/components/ui src/components/chats src/lib src/__tests__ 'src/app/\(app\)' src/components/push && pnpm typecheck && pnpm lint
```
Expected: all PASS, zero lint errors.

- [ ] **Step 2: Fingerprint unchanged**

Run:
```bash
git diff --stat origin/main -- apps/mobile/app.json apps/mobile/package.json pnpm-lock.yaml
cd apps/mobile && npx @expo/fingerprint . > /tmp/fp-branch.json
```
Never `git stash` for this (the stash stack is shared across worktrees); compute the base hash in a throwaway worktree:
```bash
git -C ../.. worktree add /tmp/fp-base origin/main && (cd /tmp/fp-base && pnpm install --frozen-lockfile >/dev/null && cd apps/mobile && npx @expo/fingerprint . > /tmp/fp-base.json)
node -e 'const a=require("/tmp/fp-base.json").hash,b=require("/tmp/fp-branch.json").hash;console.log(a,b);process.exit(a===b?0:1)'
git -C ../.. worktree remove --force /tmp/fp-base
```
Expected: the `git diff --stat` prints nothing; the two hashes are equal (exit 0).

- [ ] **Step 3: Simulator acceptance (manual, documented on the PR)**

1. `cd apps/mobile && pnpm start:dev` and, in another terminal, `pnpm ios` (dev client on the iOS simulator); sign in to a dev test workspace that has the six starter agents, at least one unread DM, one muted room, and a live Claude Code session shared from a paired Mac (start `claude` in a terminal with native sessions on).
2. Light: `xcrun simctl ui booted appearance light`; dark: `xcrun simctl ui booted appearance dark`. Capture each with `xcrun simctl io booted screenshot <name>.png`.
3. Capture, in BOTH appearances: (a) Chats top — large Lora "Chats", search bar, compose, pinned strip with the starter agents (one with a live ring while it runs), a running session first in the strip; (b) the list with an unread row (blue dot), a muted row (bell), an agent DM showing "working…" in blue, a session row with the Claude mark; (c) a row swiped both ways; (d) the long-press menu showing Pin; (e) strip reorder mode; (f) Settings with the Chats switch and Workspace rows; (g) the tab bar reading Chats · Activity · Teams · Settings.
4. Check: no text below 4.5:1 visibly, no circle avatars, no monospace anywhere, no boxed/broken glyphs, the large title collapses when the list scrolls. If the large title does not collapse or jumps when switching tabs, remove `headerLargeTitle`/`headerLargeTitleStyle`/`headerLargeTitleShadowVisible` from the Chats `useTabHeader` call (keeping the standard "Chats" title) and note it on the PR.
5. Attach the 14 screenshots to the PR description with one line each.

- [ ] **Step 4: Commit** — nothing to commit; open the PR (`feat(mobile): iPhone redesign part 1 — Chats`) with the screenshots and the fingerprint hashes, ending the body with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

---

## Self-Review

- **Spec coverage (Part 1):** tabs Chats/Activity/Teams/Settings → Task 10; Chats title/search/compose → Task 9; pinned strip, defaults = starter agents in roster order, pin/unpin/reorder, `lightbulb.pins.<orgId>`, running sessions in strip + switch default on → Tasks 5, 8, 9, 11; merged list sorted by last activity with the logic in shared → Tasks 2, 9; row anatomy (ring, name, time, two-line preview, live line in link colour, unread dot, muted bell) → Tasks 3, 4, 7; swipes Mark read / Mute / Archive → Task 7; empty/provisioning states → Task 9 (empty) + the existing `ProvisionGate` wrapping the Stack; `/sessions` + More removed, session rows open `/session/[id]`, push taps unchanged → Task 10; Settings switch + More's rows → Task 11; shared visual foundation (tokens + dark, avatar rings, vendor marks, list row) → Tasks 1, 3, 4; "no removed route is still linked" + "sessions reachable from Chats" tests → Task 10; fingerprint + screenshots → Task 12.
- **Gaps resolved by degrading:** per-row "X is typing…" (not delivered to the list), human online dots (no data), pinning a session (sessions are ephemeral; only running ones appear, automatically), "All sessions" history of ENDED sessions (the retired `/sessions?all=1` view; ended sessions stay reachable from Activity items and push taps).
- **Type consistency:** `PinKey`, `PinnedItem`, `RowParticipant`, `RosterAgent` defined in Task 5 and used unchanged in 7–9; `AvatarRing` from shared (Task 2) used by Tasks 3, 5, 7, 8; `ChatRowAction` values match the Task 9 handlers.
