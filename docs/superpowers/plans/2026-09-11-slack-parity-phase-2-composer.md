# Slack-parity messaging — Phase 2 (composer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the composer a Slack composer — inline `@` and `:` typeahead, a markdown toolbar with `⌘B/⌘I/⌘⇧X/⌘⇧C`, `↑` to edit your last message, drag-and-drop images, per-room and per-thread drafts with a Drafts row in the sidebar, an honest "who will respond" line, and the workspace keyboard map (`⌘K` quick switcher over conversations, `⌥↑/↓` room navigation, `⌥⇧↑/↓` unread navigation, `⌘/` shortcut sheet).

**Architecture:** All frontend, all inside the existing canonical room contract. Two new pure modules carry the grammar — `composerText.ts` (caret triggers, completion insertion, markdown wrapping) and `respondHint.ts` (the dispatch mirror) — and one existing pure module grows the send-time resolver (`mentions.ts` gains `resolveTypedMentions`, sharing one token pattern with `linkifyMentions` so a typed token that renders as a chip is exactly a typed token that resolves). `RoomComposer.tsx` consumes them; the checkbox mention picker is deleted, not kept beside the typeahead. Drafts are `localStorage` under the `lightbulb.messaging.` prefix, owned by `MessagingWorkspaceContext`. `⌘K` extends the already-mounted Teams command palette with a Conversations group rather than opening a second `⌘K` owner.

**Tech Stack:** Next.js 16 / React 19, `radix-ui` (Popover, DropdownMenu, Dialog — already installed), `react-markdown` via `MarkdownView`, Vitest + Testing Library (`cd apps/frontend && pnpm test -- <path>`), ESLint. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-11-slack-parity-messaging-design.md` — the "Composer" section, the "Keyboard map", the Phase 2 row of "Phases and acceptance", "States and accessibility", and "Open decisions".

## Inputs considered

**Forge's PR #2477 — "feat(messaging): autocomplete mentions from typed @" — is SUPERSEDED by Task 2. It should be closed as superseded, not merged.** It is good prior art for the trigger/keyboard shape and its five test scenarios are reused below, but its behaviour contradicts the spec on four points that are not patches, they are the design:

1. **Picking a human inserts no text.** `const token = member.kind === "agent" && member.mentionHandle ? "@handle" : ""` deletes the typed `@` and leaves only a structured mention; its own test asserts `toHaveValue("Hi ")`. The spec's Open decision 1 is *resolved*: the human token in text is `@Display Name`, resolved client-side.
2. **No send-time resolution.** Spec line 98 is explicit — "a typed `@friday` must work exactly like picking Friday from the typeahead". #2477 only adds a mention when the popover is used; typing `@friday ` past the trigger sends with an empty `mentions[]`, and the backend only honours `mentions[]`.
3. **Its query charset cannot express the resolved decision.** `/(^|\s)@([a-z0-9_-]*)$/i` can never match `@Aden Kim`.
4. **The checkbox picker survives as a second mode** and the `@` button still opens it. Spec: "Replaces the checkbox picker; the `@` button opens the same popover."

Do not merge or modify #2477 from this plan.

## Assumptions (open decisions and spec gaps, resolved to the spec's stated default)

- **Open decision 3 — `⌘K`: RESOLVED to reuse the Teams command palette.** `CommandPaletteProvider` is mounted in `WorkspaceShell.tsx:449`, *inside* `DesktopMessagingProvider` (line 448), already owns the global `⌘K` keydown, and already indexes nav actions + agents + issues + projects. A messaging-local `⌘K` would be a second toggler of the same chord. Task 8 adds a Conversations group. **Coordination flag:** `components/teams/command-palette/**` is outside the Claude-owned file list in the spec's Coordination section; the diff there is additive (one group, one optional `run` field on `FlatItem`) and touches no Teams data path.
- **Typeahead popovers are the ARIA combobox pattern, not a `radix-ui` Popover.** "States and accessibility" says popovers are radix with roving focus and Escape. A caret-anchored typeahead must leave focus in the textarea, so both typeaheads render as an in-flow `role="listbox"` above the composer with `aria-activedescendant` roving, `aria-expanded`/`aria-controls` on the textarea, and Escape — the requirement satisfied, the primitive not used. `@/components/ui/popover` exports no `PopoverAnchor`, so using radix here would also mean adding a primitive export for zero a11y gain. The emoji *button* picker stays the shipped radix `Popover`; overflow menus stay `DropdownMenu`.
- **Spec internal contradiction on unmentioned `mentions`-mode agents.** The Composer section says both "`mentions` agents 'reply when mentioned'" and "unmentioned `mentions` agents get nothing", while its own compact example reads "Friday will reply · **Gauge when mentioned** · Atlas observing". Task 7 follows the compact example: an unmentioned `mentions` agent gets the phrase `<Name> when mentioned`. "Get nothing" is read as "gets no dispatch".
- **`↑` in an empty composer is NOT shipped, contrary to the spec.** Spec line 99 credits it to #2473. #2473 shipped edit/delete via `RoomMessageContent`'s local `request` state; there is no `ArrowUp` handler anywhere in `components/messaging/`. It is built here, in Task 5.
- **Backward history paging stays out.** Spec defers `beforeSeq` to Phase 3.

## Global Constraints

- No colour literals outside `globals.css` — tokens only (`var(--foreground)`, `var(--muted-foreground)`, `var(--border)`, `var(--input)`, `var(--ring)`, `var(--accent)`, …). `node scripts/brand-tokens-scan.mjs src` must print `0` (run from `apps/frontend`).
- Borders only, no shadows except the shipped `.room-message-actions` one and the radix popover/dialog pop; 4px spacing rhythm; radii from the existing `messaging.css` (4–8px, composer 10px); Source Sans via `var(--font-sans)`.
- "Never colour alone": every status keeps its word or icon. State colours mark text/icon/border/rule, never fills.
- Every new control has hover, focus-visible, disabled, pending, and error states. The global focus recipe is the only one (`outline: 2px solid var(--ring); outline-offset: 2px` on `:focus-visible`); the composer card swaps its border to `ring` on `:focus-within` instead. Touch targets ≥44px at ≤859px (the existing `@media (max-width: 859px)` rule already forces `min-height: 44px` on `.room-panel button`).
- `prefers-reduced-motion: reduce` is already honoured by the blanket rule under `.room-panel *`; add no new animation that escapes it.
- **No new dependencies.** **No feature flags, no migration periods, no dual-write paths, no compatibility shims** — the checkbox mention picker is deleted, not kept beside the typeahead.
- Vitest + Testing Library. Any clock-dependent fixture uses `vi.useFakeTimers({ toFake: ["Date"] })` with local-time constructors (`new Date(2026, 8, 11, 10, 0)`), never a `Z`-suffixed literal; tests must pass in any host timezone.
- `localStorage` keys stay under the `lightbulb.messaging.` prefix (existing: `lightbulb.messaging.recentEmoji`). Every read and write is wrapped in `try/catch` and the component renders correctly with no stored value.
- Never log message bodies. Never send message text anywhere new. Drafts never leave the browser.
- **Never use `sleep` or `tail` in bash.** Use `gh run watch` for CI and the Read tool for files.
- Ownership: Claude's messaging lane. Do not touch onboarding, billing, provisioning, or backend messaging in this plan.
- Existing tests in `apps/frontend/src/components/messaging/__tests__/`, `apps/frontend/src/components/workspace/__tests__/`, and `apps/frontend/src/hooks/__tests__/useRooms.test.tsx` must keep passing after every task.
- Commits: conventional prefix (`feat(messaging): …`, `refactor(messaging): …`), ending with the attribution block from the session's system reminder.

---

## File structure

| File | Responsibility |
|---|---|
| `apps/frontend/src/components/messaging/composerText.ts` (create) | Pure caret grammar: `activeTrigger`, `applyCompletion`, `wrapSelection`. No React, no DOM. |
| `apps/frontend/src/components/messaging/mentions.ts` (modify) | Gains `resolveTypedMentions`; `linkifyMentions` refactored onto the same shared token pattern so chip rendering and send-time resolution can never diverge. |
| `apps/frontend/src/components/messaging/respondHint.ts` (create) | Pure mirror of `select_dispatch_targets`: `respondHint(roomKind, members, mentions, runningAgentIds) → string`. |
| `apps/frontend/src/components/messaging/drafts.ts` (create) | `localStorage` drafts: `draftKey`, `readDraft`, `writeDraft`, `clearDraft`, `listDraftRoomIds`. |
| `apps/frontend/src/components/messaging/roomCommands.ts` (create) | `useRoomCommandResults(query, enabled)` — rooms + people/agents for the command palette, degrading to `[]` with no messaging provider. |
| `apps/frontend/src/components/messaging/ShortcutSheet.tsx` (create) | The `⌘/` keyboard-map dialog. |
| `apps/frontend/src/components/messaging/RoomComposer.tsx` (modify) | Typeaheads, formatting toolbar + shortcuts, `↑`-to-edit, drag-and-drop, send-time mention resolution, the respond hint. |
| `apps/frontend/src/components/messaging/RoomPanel.tsx` (modify) | Passes `roomKind`, `runningAgentIds`, `onEditLast`/`editTargetId` through; drop zone over the message list. |
| `apps/frontend/src/components/messaging/RoomMessageItem.tsx` (modify) | Threads `startEditing` to `RoomMessageContent`. |
| `apps/frontend/src/components/messaging/RoomMessageContent.tsx` (modify) | Opens its edit form when `startEditing` flips true. |
| `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (modify) | Draft restore/persist, `draftRoomIds`, `useMessagingWorkspaceOptional`, the shortcut hook + `ShortcutSheet` mount, `rooms`/`selectRoom` on the context value. |
| `apps/frontend/src/components/messaging/RoomDirectory.tsx` (modify) | Drafts section + the pencil glyph on draft rows. |
| `apps/frontend/src/components/messaging/messaging.css` (modify) | Typeahead list, formatting toolbar, drop-zone, respond hint, draft pencil. |
| `apps/frontend/src/components/teams/command-palette/CommandPalette.tsx` (modify) | Conversations group; `FlatItem.run` for non-URL actions. |
| `apps/infra/openclaw/skills/lightbulb/references/surfaces.md` + `MAINTENANCE.md` (modify) | Chat row (Task 7) and Workspace search row (Task 8). |
| Tests | `__tests__/composerText.test.ts`, `__tests__/mentions.test.ts` (add), `__tests__/RoomComposer.typeahead.test.tsx`, `__tests__/RoomComposer.format.test.tsx`, `__tests__/RoomComposer.editLast.test.tsx`, `__tests__/RoomComposer.dnd.test.tsx`, `__tests__/drafts.test.ts`, `__tests__/MessagingWorkspaceContext.test.tsx` (add), `__tests__/respondHint.test.ts`, `__tests__/roomCommands.test.tsx`, `__tests__/RoomDirectory.test.tsx` (add), `__tests__/ShortcutSheet.test.tsx`. |

Types reused throughout (`@lightbulb/shared`): `MemberRef { kind: "human" | "agent"; id: string }`, `ListenMode = "active" | "mentions" | "observe"`, `RoomKind = "channel" | "dm" | "group-dm"`, `CreateMessageRequest { clientMessageId, text, mentions: MemberRef[], replyToMessageId, threadRootMessageId, attachmentIds? }`. From `components/messaging/types.ts`: `RoomParticipant` (`kind`, `id`, `displayName`, `mentionHandle`, `role`, `listenMode`, `joinedAt`, `joinedSeq`, `agentActivity?`), `DirectoryRoom`, `DirectoryParticipant`, `RoomRunView`.

---

# PR A — the composer (Tasks 1–6)

### Task 1: Caret grammar and send-time mention resolution (pure)

*Model: haiku — complete code and complete tests are given; this is transcription.*

**Files:**
- Create: `apps/frontend/src/components/messaging/composerText.ts`
- Modify: `apps/frontend/src/components/messaging/mentions.ts`
- Test: `apps/frontend/src/components/messaging/__tests__/composerText.test.ts` (create)
- Test: `apps/frontend/src/components/messaging/__tests__/mentions.test.ts` (append)

**Interfaces:**
- Produces:
  ```ts
  // composerText.ts
  export type TriggerKind = "mention" | "emoji";
  export interface CompletionTrigger { kind: TriggerKind; start: number; end: number; query: string }
  export function activeTrigger(value: string, caret: number | null): CompletionTrigger | null;
  export function applyCompletion(value: string, trigger: CompletionTrigger, token: string): { text: string; caret: number };
  export type MarkdownStyle = "bold" | "italic" | "strike" | "code" | "codeblock" | "ul" | "ol" | "link";
  export function wrapSelection(value: string, start: number, end: number, style: MarkdownStyle): { text: string; selectionStart: number; selectionEnd: number };

  // mentions.ts (new export; MENTION_SCHEME, parseMentionHref, linkifyMentions keep their current signatures)
  export function resolveTypedMentions(text: string, participants: readonly Target[]): MemberRef[];
  ```
  `start` is the index of the `@`/`:` sigil; `end` is the caret. `query` excludes the sigil. A mention query may carry up to two internal spaces (so `@Aden Kim` is reachable); the *caller* closes the list when nothing matches, which is what stops ordinary prose after an `@` holding a dead popover open.

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/components/messaging/__tests__/composerText.test.ts
import { describe, expect, it } from "vitest";
import { activeTrigger, applyCompletion, wrapSelection } from "../composerText";

describe("activeTrigger", () => {
  it("opens a mention trigger at the start of the field and after whitespace", () => {
    expect(activeTrigger("@", 1)).toEqual({ kind: "mention", start: 0, end: 1, query: "" });
    expect(activeTrigger("Ask @fri", 8)).toEqual({ kind: "mention", start: 4, end: 8, query: "fri" });
  });
  it("carries up to two internal spaces so a display name is reachable", () => {
    expect(activeTrigger("Hi @Aden Kim", 12)).toEqual({ kind: "mention", start: 3, end: 12, query: "Aden Kim" });
    expect(activeTrigger("Hi @Aden Kim the third", 22)).toBeNull();
  });
  it("ignores an @ glued to a word and a caret before the sigil", () => {
    expect(activeTrigger("mail@example", 12)).toBeNull();
    expect(activeTrigger("Ask @fri", 3)).toBeNull();
    expect(activeTrigger("Ask @fri", null)).toBeNull();
  });
  it("opens an emoji trigger only after two characters", () => {
    expect(activeTrigger("nice :t", 7)).toBeNull();
    expect(activeTrigger("nice :ta", 8)).toEqual({ kind: "emoji", start: 5, end: 8, query: "ta" });
    expect(activeTrigger("nice :tada", 10)).toEqual({ kind: "emoji", start: 5, end: 10, query: "tada" });
  });
});

describe("applyCompletion", () => {
  it("replaces the trigger and leaves the caret after a single trailing space", () => {
    const trigger = activeTrigger("Ask @fri", 8)!;
    expect(applyCompletion("Ask @fri", trigger, "@friday")).toEqual({ text: "Ask @friday ", caret: 12 });
  });
  it("does not double the space when text already follows", () => {
    const trigger = activeTrigger("Ask @fri", 8)!;
    expect(applyCompletion("Ask @fri now", trigger, "@friday")).toEqual({ text: "Ask @friday now", caret: 11 });
  });
});

describe("wrapSelection", () => {
  it("wraps and unwraps the inline pairs", () => {
    expect(wrapSelection("ship it", 0, 4, "bold")).toEqual({ text: "**ship** it", selectionStart: 2, selectionEnd: 6 });
    expect(wrapSelection("**ship** it", 2, 6, "bold")).toEqual({ text: "ship it", selectionStart: 0, selectionEnd: 4 });
    expect(wrapSelection("ship", 0, 4, "italic").text).toBe("_ship_");
    expect(wrapSelection("ship", 0, 4, "strike").text).toBe("~~ship~~");
    expect(wrapSelection("ship", 0, 4, "code").text).toBe("`ship`");
  });
  it("fences a code block and selects its body", () => {
    expect(wrapSelection("ship", 0, 4, "codeblock")).toEqual({ text: "```\nship\n```", selectionStart: 4, selectionEnd: 8 });
  });
  it("prefixes every line of the touched block for lists", () => {
    expect(wrapSelection("one\ntwo", 0, 7, "ul").text).toBe("- one\n- two");
    expect(wrapSelection("one\ntwo", 0, 7, "ol").text).toBe("1. one\n2. two");
  });
  it("builds a link and selects the url placeholder", () => {
    expect(wrapSelection("docs", 0, 4, "link")).toEqual({ text: "[docs](url)", selectionStart: 7, selectionEnd: 10 });
    expect(wrapSelection("", 0, 0, "link").text).toBe("[text](url)");
  });
});
```

```ts
// append to apps/frontend/src/components/messaging/__tests__/mentions.test.ts
import { resolveTypedMentions } from "../mentions";

describe("resolveTypedMentions", () => {
  const participants = [
    { kind: "agent" as const, id: "agent-friday", displayName: "Friday", mentionHandle: "friday" },
    { kind: "human" as const, id: "user-aden", displayName: "Aden Kim", mentionHandle: null },
  ];
  it("resolves a typed agent handle and a typed human display name", () => {
    expect(resolveTypedMentions("@friday can you ping @Aden Kim", participants)).toEqual([
      { kind: "agent", id: "agent-friday" },
      { kind: "human", id: "user-aden" },
    ]);
  });
  it("is case-insensitive and de-duplicates", () => {
    expect(resolveTypedMentions("@Friday @friday", participants)).toEqual([{ kind: "agent", id: "agent-friday" }]);
  });
  it("ignores tokens inside code spans and fences, and unknown tokens", () => {
    expect(resolveTypedMentions("`@friday` and ```\n@friday\n``` and @nobody", participants)).toEqual([]);
  });
  it("returns an empty list when the room has no mentionable members", () => {
    expect(resolveTypedMentions("@friday", [])).toEqual([]);
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/composerText.test.ts src/components/messaging/__tests__/mentions.test.ts`
Expected: FAIL — `Cannot find module '../composerText'` and `resolveTypedMentions is not a function`.

- [ ] **Step 3: Write `composerText.ts`**

```ts
// apps/frontend/src/components/messaging/composerText.ts
export type TriggerKind = "mention" | "emoji";

export interface CompletionTrigger {
  /** "mention" for `@`, "emoji" for `:`. */
  kind: TriggerKind;
  /** Index of the sigil itself. */
  start: number;
  /** The caret. */
  end: number;
  /** Everything typed after the sigil. */
  query: string;
}

// A mention query may carry up to two internal spaces so `@Aden Kim` is
// reachable (Open decision 1: the human token in text is `@Display Name`).
// Nothing here rejects prose — the caller closes the list when no member
// matches, which is the only rule that generalises to every display name.
const MENTION_RE = /(?:^|\s)@([^\s@:]*(?: [^\s@:]*){0,2})$/;
// Two characters minimum, per the spec's "`:` emoji typeahead after two characters".
const EMOJI_RE = /(?:^|\s):([a-z0-9_+-]{2,})$/i;

export function activeTrigger(value: string, caret: number | null): CompletionTrigger | null {
  if (caret === null) return null;
  const head = value.slice(0, caret);
  const emoji = EMOJI_RE.exec(head);
  if (emoji) return { kind: "emoji", start: caret - emoji[1].length - 1, end: caret, query: emoji[1] };
  const mention = MENTION_RE.exec(head);
  if (mention) return { kind: "mention", start: caret - mention[1].length - 1, end: caret, query: mention[1] };
  return null;
}

export function applyCompletion(value: string, trigger: CompletionTrigger, token: string): { text: string; caret: number } {
  const tail = value.slice(trigger.end);
  const separator = tail.startsWith(" ") ? "" : " ";
  return {
    text: `${value.slice(0, trigger.start)}${token}${separator}${tail}`,
    caret: trigger.start + token.length + separator.length,
  };
}

export type MarkdownStyle = "bold" | "italic" | "strike" | "code" | "codeblock" | "ul" | "ol" | "link";

const PAIRS: Record<"bold" | "italic" | "strike" | "code", string> = {
  bold: "**",
  italic: "_",
  strike: "~~",
  code: "`",
};

export function wrapSelection(
  value: string,
  start: number,
  end: number,
  style: MarkdownStyle,
): { text: string; selectionStart: number; selectionEnd: number } {
  const selected = value.slice(start, end);
  if (style === "ul" || style === "ol") {
    const lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
    const newline = value.indexOf("\n", end);
    const lineEnd = newline === -1 ? value.length : newline;
    const block = value
      .slice(lineStart, lineEnd)
      .split("\n")
      .map((line, index) => `${style === "ol" ? `${index + 1}. ` : "- "}${line}`)
      .join("\n");
    return {
      text: `${value.slice(0, lineStart)}${block}${value.slice(lineEnd)}`,
      selectionStart: lineStart,
      selectionEnd: lineStart + block.length,
    };
  }
  if (style === "codeblock") {
    return {
      text: `${value.slice(0, start)}\`\`\`\n${selected}\n\`\`\`${value.slice(end)}`,
      selectionStart: start + 4,
      selectionEnd: start + 4 + selected.length,
    };
  }
  if (style === "link") {
    const label = selected || "text";
    const urlStart = start + label.length + 3;
    return {
      text: `${value.slice(0, start)}[${label}](url)${value.slice(end)}`,
      selectionStart: urlStart,
      selectionEnd: urlStart + 3,
    };
  }
  const marker = PAIRS[style];
  if (value.slice(Math.max(0, start - marker.length), start) === marker && value.slice(end, end + marker.length) === marker) {
    return {
      text: `${value.slice(0, start - marker.length)}${selected}${value.slice(end + marker.length)}`,
      selectionStart: start - marker.length,
      selectionEnd: end - marker.length,
    };
  }
  return {
    text: `${value.slice(0, start)}${marker}${selected}${marker}${value.slice(end)}`,
    selectionStart: start + marker.length,
    selectionEnd: end + marker.length,
  };
}
```

- [ ] **Step 4: Rewrite `mentions.ts` onto one shared token pattern**

Replace the whole file with:

```ts
// apps/frontend/src/components/messaging/mentions.ts
import type { MemberRef } from "@lightbulb/shared";
import type { RoomParticipant } from "./types";

export const MENTION_SCHEME = "mention:";
type Target = Pick<RoomParticipant, "kind" | "id" | "displayName" | "mentionHandle">;

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

interface TokenEntry {
  participant: Target;
  token: string;
}

// ONE grammar for both readers: what renders as a chip is exactly what
// resolves to a structured mention on send. Split them and a typed token can
// look mentioned and notify nobody.
function tokenEntries(participants: readonly Target[]): TokenEntry[] {
  return participants
    .map((participant) => ({
      participant,
      token: participant.kind === "agent" ? participant.mentionHandle : participant.displayName,
    }))
    .filter((entry): entry is TokenEntry => !!entry.token)
    .sort((left, right) => right.token.length - left.token.length);
}

const tokenPattern = (entries: readonly TokenEntry[]) =>
  new RegExp(`(^|[\\s(])@(${entries.map((entry) => escapeRegExp(entry.token)).join("|")})(?![\\w.-]|\\]\\()`, "gi");

// Even segments are prose; odd segments are code (inline `…` or fenced ```…```).
const proseSegments = (text: string) => text.split(/(```[\s\S]*?```|`[^`\n]*`)/);

export function parseMentionHref(href: string): { kind: "human" | "agent"; id: string } | null {
  if (!href.startsWith(MENTION_SCHEME)) return null;
  const [kind, ...rest] = href.slice(MENTION_SCHEME.length).split(":");
  const id = rest.join(":");
  return (kind === "human" || kind === "agent") && id ? { kind, id } : null;
}

export function linkifyMentions(
  text: string,
  // Unused: the composer resolves typed @tokens to structured mentions on
  // send. This rewrite links any member token found in the text regardless of
  // whether it is also present here as a structured mention.
  mentions: readonly MemberRef[],
  participants: readonly Target[],
): string {
  const entries = tokenEntries(participants);
  if (entries.length === 0) return text;
  const pattern = tokenPattern(entries);
  const rewrite = (segment: string) =>
    segment.replace(pattern, (match, lead: string, token: string) => {
      const entry = entries.find((candidate) => candidate.token.toLowerCase() === token.toLowerCase());
      if (!entry) return match;
      return `${lead}[@${token}](${MENTION_SCHEME}${entry.participant.kind}:${entry.participant.id})`;
    });
  return proseSegments(text)
    .map((segment, index) => (index % 2 === 0 ? rewrite(segment) : segment))
    .join("");
}

/** Every room member named by a plain `@token` in the prose of `text`. */
export function resolveTypedMentions(text: string, participants: readonly Target[]): MemberRef[] {
  const entries = tokenEntries(participants);
  if (entries.length === 0) return [];
  const pattern = tokenPattern(entries);
  const found: MemberRef[] = [];
  proseSegments(text).forEach((segment, index) => {
    if (index % 2 !== 0) return;
    for (const match of segment.matchAll(pattern)) {
      const entry = entries.find((candidate) => candidate.token.toLowerCase() === match[2].toLowerCase());
      if (!entry) continue;
      const ref = { kind: entry.participant.kind, id: entry.participant.id };
      if (!found.some((existing) => existing.kind === ref.kind && existing.id === ref.id)) found.push(ref);
    }
  });
  return found;
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/composerText.test.ts src/components/messaging/__tests__/mentions.test.ts`
Expected: PASS, and every pre-existing `mentions.test.ts` case still green.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/messaging/composerText.ts apps/frontend/src/components/messaging/mentions.ts apps/frontend/src/components/messaging/__tests__/composerText.test.ts apps/frontend/src/components/messaging/__tests__/mentions.test.ts
git commit -m "feat(messaging): caret grammar and send-time mention resolution"
```

---

### Task 2: `@` typeahead replaces the checkbox picker; typed mentions resolve on send

*Model: sonnet — integration into a 260-line stateful component with an existing send state machine.*

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx`
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomComposer.typeahead.test.tsx` (create)

**Interfaces:**
- Consumes: `activeTrigger`, `applyCompletion` (Task 1, `./composerText`); `resolveTypedMentions` (Task 1, `./mentions`).
- Produces: `RoomComposer` renders `role="listbox"` `aria-label="Mention a room member"` while a mention trigger is open, with one `role="option"` per match; the textarea carries `aria-autocomplete="list"`, `aria-expanded`, `aria-controls={`${fieldId}-targets`}`, `aria-activedescendant={`${fieldId}-option-<index>`}`. The checkbox `fieldset` and the `query`/`pickerOpen`/`toggleMention`/`visibleMembers` members are **deleted**. `request()` now returns `mentions` = `selectedMentions` ∪ `resolveTypedMentions(text, members)`.

**Behaviour (spec, "Composer"):** typing `@` opens a list of room members — **agents first when the room is a DM with one agent** (that is exactly the `focusAgentId` prop the context already passes), otherwise room order. Arrow keys move the active option (wrapping), Enter or Tab inserts, Escape closes without changing the draft, click inserts. Inserting an agent writes `@<mentionHandle>`, a human writes `@<Display Name>`, and both add the structured mention. The list closes the moment nothing matches, so prose after an `@` never holds it open. Enter with the list closed still sends. The `@` button opens the same list with an empty query at the caret.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/frontend/src/components/messaging/__tests__/RoomComposer.typeahead.test.tsx
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MemberRef } from "@lightbulb/shared";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import type { RoomParticipant } from "../types";

const members: RoomParticipant[] = [
  { kind: "human", id: "user-aden", displayName: "Aden Kim", mentionHandle: null, role: "member", listenMode: null, joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 1 },
  { kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 2 },
  { kind: "agent", id: "agent-atlas", displayName: "Atlas", mentionHandle: "atlas", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 3 },
];

function Harness({ onSubmit = vi.fn() }: { onSubmit?: RoomComposerProps["onSubmit"] }) {
  const [text, setText] = useState("");
  const [mentions, setMentions] = useState<MemberRef[]>([]);
  return <RoomComposer text={text} onTextChange={setText} members={members} selectedMentions={mentions}
    onMentionsChange={setMentions} clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
}

const field = () => screen.getByRole("textbox", { name: "Message" });
const type = (value: string) => fireEvent.change(field(), { target: { value, selectionStart: value.length } });

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("opens and filters the mention list from a typed @", () => {
  render(<Harness />);
  type("Ask @fri");
  expect(screen.getByRole("listbox", { name: "Mention a room member" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /Friday/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Atlas/ })).not.toBeInTheDocument();
});

it("closes the list when nothing matches so prose after an @ is left alone", () => {
  render(<Harness />);
  type("Email me @ the office");
  expect(screen.queryByRole("listbox", { name: "Mention a room member" })).not.toBeInTheDocument();
});

it("inserts an agent handle with Enter instead of sending", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Ask @fri");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(field()).toHaveValue("Ask @friday ");
  expect(onSubmit).not.toHaveBeenCalled();
});

it("inserts a human as @Display Name and sends the structured mention", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Hi @Ade");
  fireEvent.click(screen.getByRole("option", { name: /Aden Kim/ }));
  expect(field()).toHaveValue("Hi @Aden Kim ");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "Hi @Aden Kim ",
    mentions: [{ kind: "human", id: "user-aden" }],
  }));
});

it("resolves a fully typed handle on send without ever opening the list", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("@friday ship it");
  expect(screen.queryByRole("listbox", { name: "Mention a room member" })).not.toBeInTheDocument();
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "@friday ship it",
    mentions: [{ kind: "agent", id: "agent-friday" }],
  }));
});

it("leaves an unmatched token plain and mentions nobody", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("@nobody ship it");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ mentions: [] }));
});

it("moves the active option with the arrow keys and wraps", () => {
  render(<Harness />);
  type("Ask @");
  const id = () => field().getAttribute("aria-activedescendant");
  const first = id();
  fireEvent.keyDown(field(), { key: "ArrowDown" });
  expect(id()).not.toBe(first);
  fireEvent.keyDown(field(), { key: "ArrowUp" });
  expect(id()).toBe(first);
});

it("inserts with Tab as well as Enter", () => {
  render(<Harness />);
  type("Ask @atl");
  fireEvent.keyDown(field(), { key: "Tab" });
  expect(field()).toHaveValue("Ask @atlas ");
});

it("closes with Escape without changing the draft", () => {
  render(<Harness />);
  type("Ask @");
  fireEvent.keyDown(field(), { key: "Escape" });
  expect(screen.queryByRole("listbox", { name: "Mention a room member" })).not.toBeInTheDocument();
  expect(field()).toHaveValue("Ask @");
});

it("opens the same list from the @ button", () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Mention someone" }));
  expect(screen.getByRole("listbox", { name: "Mention a room member" })).toBeInTheDocument();
  expect(field()).toHaveValue("@");
});

it("still sends on Enter when no list is open", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Ship it");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledOnce();
});
```

- [ ] **Step 2: Run to verify the tests fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomComposer.typeahead.test.tsx`
Expected: FAIL — no `listbox`, no `Mention someone` button.

- [ ] **Step 3: Rework `RoomComposer.tsx`**

Imports — add, beside the existing ones:
```tsx
import { activeTrigger, applyCompletion, type CompletionTrigger } from "./composerText";
import { resolveTypedMentions } from "./mentions";
import { ParticipantAvatar } from "./ParticipantAvatar";
```

Delete `const [pickerOpen, setPickerOpen] = useState(false);`, `const [query, setQuery] = useState("");`, the `toggleMention` function, the `visibleMembers` const, and the whole `{pickerOpen && <fieldset …>…</fieldset>}` block. Add:

```tsx
const [trigger, setTrigger] = useState<CompletionTrigger | null>(null);
const [activeIndex, setActiveIndex] = useState(0);
```

Replace the `mentions`/`authorized` block (currently lines 108–111) with:

```tsx
  const defaultMention = defaultAgentId ? { kind: "agent" as const, id: defaultAgentId } : null;
  // A DM with exactly one agent puts that agent first — the same signal the
  // context already uses for focusAgentId.
  const mentionable = focusAgentId
    ? [...members.filter((member) => member.id === focusAgentId), ...members.filter((member) => member.id !== focusAgentId)]
    : members;
  const options = trigger?.kind === "mention"
    ? mentionable.filter((member) => {
        const needle = trigger.query.toLocaleLowerCase();
        return !needle
          || member.displayName.toLocaleLowerCase().includes(needle)
          || (member.mentionHandle ?? "").toLocaleLowerCase().includes(needle);
      })
    : [];
  const mentionOpen = options.length > 0;
  const mentions = [...selectedMentions];
  for (const ref of resolveTypedMentions(text, members)) {
    if (!mentions.some((mention) => sameMember(mention, ref))) mentions.push(ref);
  }
  if (defaultMention && !mentions.some((mention) => sameMember(mention, defaultMention))) mentions.push(defaultMention);
  const authorized = mentions.every((mention) => members.some((member) => sameMember(member, mention)));
```

Add the two helpers below `removeImage`:

```tsx
  function syncTrigger(value: string, caret: number | null) {
    if (composing.current) return;
    setTrigger(activeTrigger(value, caret));
    setActiveIndex(0);
  }

  function choose(member: RoomParticipant) {
    if (!trigger) return;
    const token = member.kind === "agent" && member.mentionHandle ? `@${member.mentionHandle}` : `@${member.displayName}`;
    const next = applyCompletion(text, trigger, token);
    const ref: MemberRef = { kind: member.kind, id: member.id };
    if (!selectedMentions.some((mention) => sameMember(mention, ref)) && selectedMentions.length < 32) {
      onMentionsChange([...selectedMentions, ref]);
    }
    onTextChange(next.text);
    setTrigger(null);
    requestAnimationFrame(() => {
      field.current?.focus();
      field.current?.setSelectionRange(next.caret, next.caret);
    });
  }
```

Textarea: add the combobox attributes and wire the caret handlers.

```tsx
    <Textarea ref={field} id={fieldId} value={text} disabled={!editable} rows={3} aria-invalid={!!error}
      aria-describedby={`${fieldId}-help ${fieldId}-status`} placeholder={label === "Reply to thread" ? "Reply to thread…" : "Message the conversation · @ to mention"}
      aria-autocomplete="list" aria-expanded={mentionOpen} aria-controls={mentionOpen ? `${fieldId}-targets` : undefined}
      aria-activedescendant={mentionOpen ? `${fieldId}-option-${activeIndex}` : undefined}
      onChange={(event) => { onTextChange(event.target.value); onTypingChange?.(!!event.target.value.trim()); syncTrigger(event.target.value, event.target.selectionStart); }}
      onClick={(event) => syncTrigger(event.currentTarget.value, event.currentTarget.selectionStart)}
      onKeyUp={(event) => { if (event.key.startsWith("Arrow") || event.key === "Home" || event.key === "End") syncTrigger(event.currentTarget.value, event.currentTarget.selectionStart); }}
      onPaste={(event) => {
        const files = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
        if (files.length) { event.preventDefault(); addImages(files); }
      }}
      onBlur={() => onTypingChange?.(false)} onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={(event) => { composing.current = false; syncTrigger(event.currentTarget.value, event.currentTarget.selectionStart); }}
      onKeyDown={(event) => {
        if (mentionOpen) {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setActiveIndex((index) => (index + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
            return;
          }
          if (event.key === "Enter" || event.key === "Tab") {
            if (event.nativeEvent.isComposing || event.keyCode === 229) return;
            event.preventDefault();
            choose(options[activeIndex] ?? options[0]);
            return;
          }
          if (event.key === "Escape") {
            // Esc closes the typeahead FIRST; the thread/overlay listener on
            // window must not also fire. stopPropagation on the synthetic
            // event is not enough — React listens at the root, below window.
            event.preventDefault();
            event.nativeEvent.stopImmediatePropagation();
            setTrigger(null);
            return;
          }
        }
        if (event.key === "Enter" && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          submit();
        }
      }} />
```

Replace the `@` button in `.room-composer-actions` with one that seeds the trigger at the caret:

```tsx
      <Button type="button" variant="ghost" aria-label="Mention someone" aria-expanded={mentionOpen}
        disabled={!editable} onClick={() => {
          const caret = field.current?.selectionStart ?? text.length;
          const needsSpace = caret > 0 && !/\s$/.test(text.slice(0, caret));
          const next = `${text.slice(0, caret)}${needsSpace ? " " : ""}@${text.slice(caret)}`;
          const sigil = caret + (needsSpace ? 1 : 0);
          onTextChange(next);
          setTrigger({ kind: "mention", start: sigil, end: sigil + 1, query: "" });
          setActiveIndex(0);
          requestAnimationFrame(() => { field.current?.focus(); field.current?.setSelectionRange(sigil + 1, sigil + 1); });
        }}><AtSign aria-hidden="true" /><span className="sr-only">Mention</span></Button>
```

Render the list **above** the textarea (first child of the form, so the DOM order matches the visual order for screen readers):

```tsx
    {mentionOpen && <div id={`${fieldId}-targets`} className="room-typeahead" role="listbox" aria-label="Mention a room member">
      {options.map((member, index) => <button key={`${member.kind}:${member.id}`} id={`${fieldId}-option-${index}`} type="button" role="option"
        aria-selected={index === activeIndex} className="room-choice room-choice-button" data-active={index === activeIndex || undefined}
        onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(member)}>
        <ParticipantAvatar participant={member} size="sm" />
        <span className="min-w-0 flex-1 text-left"><span className="block truncate" title={member.displayName}>{member.displayName}</span>
          <span className="text-muted-foreground text-sm">{member.kind === "agent" && member.mentionHandle ? `@${member.mentionHandle}` : "Person"}</span></span>
      </button>)}
    </div>}
```

- [ ] **Step 4: Add the CSS**

Append to `messaging.css`, after the `.room-picker-list` rule:

```css
.room-composer { position: relative; }
.room-typeahead { position: absolute; left: 0; right: 0; bottom: calc(100% + 4px); z-index: 4; max-height: 240px; overflow-y: auto; overscroll-behavior: contain; padding: 4px; border: 1px solid var(--border); border-radius: 8px; background: var(--popover); box-shadow: var(--shadow-pop); }
.room-typeahead .room-choice-button[data-active] { background: var(--accent); }
```

(`.room-composer` already sets `position: relative` at line 56 — keep the single declaration there and delete the duplicate if the linter flags it.)

- [ ] **Step 5: Run the tests, the sibling suites, lint and the scanner**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`. `RoomComposer.focus.test.tsx` and `RoomPanel.test.tsx` must stay green — if either asserted on the deleted "Choose mentions" button or the "Room members" fieldset, update those assertions to the new listbox in this task.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/messaging/RoomComposer.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__
git commit -m "feat(messaging): inline @ typeahead and send-time mention resolution"
```

---

### Task 3: `:` emoji typeahead, and the picker button inserts at the caret

*Model: sonnet — same component, second trigger kind, shares the caret machinery.*

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomComposer.typeahead.test.tsx` (append)

**Interfaces:**
- Consumes: `activeTrigger`/`applyCompletion` (Task 1), `searchEmoji(query): EmojiEntry[]` and `EmojiPicker` (shipped, `./emojiData` / `./EmojiPicker`; `EmojiPickerProps = { onPick: (emoji: string) => void; disabled?: boolean; triggerLabel?: string }`).
- Produces: a second `role="listbox"` with `aria-label="Insert an emoji"`; the same `activeIndex` state serves both trigger kinds.

**Behaviour:** typing `:` plus two characters lists matching emoji (name or keyword, `searchEmoji` already caps at 48 — show the first 8). Arrows/Enter/Tab/Escape behave exactly as the mention list. Choosing one replaces `:query` with the emoji character and records it as recent (`recordRecentEmoji`, shipped in `EmojiPicker.tsx`). The composer's picker **button** is the shipped `EmojiPicker` popover and inserts at the caret rather than appending.

- [ ] **Step 1: Append the failing tests**

```tsx
it("opens the emoji list after two characters and inserts the character", () => {
  render(<Harness />);
  type("nice :ta");
  expect(screen.getByRole("listbox", { name: "Insert an emoji" })).toBeInTheDocument();
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(field().value.startsWith("nice 🎉")).toBe(true);
});

it("does not open the emoji list after one character", () => {
  render(<Harness />);
  type("nice :t");
  expect(screen.queryByRole("listbox", { name: "Insert an emoji" })).not.toBeInTheDocument();
});

it("inserts a picked emoji at the caret, not at the end", () => {
  render(<Harness />);
  type("ab");
  fireEvent.change(field(), { target: { value: "ab", selectionStart: 1 } });
  fireEvent.click(screen.getByRole("button", { name: "Insert emoji" }));
  fireEvent.click(screen.getByRole("button", { name: "party popper" }));
  expect(field()).toHaveValue("a🎉b");
});
```

> The emoji asserted is `🎉`, whose entry in `emojiData.ts` is named `party popper` with the keyword `tada`. Confirm both strings with `grep -n "party popper" apps/frontend/src/components/messaging/emojiData.ts` before running; if the curated set names it differently, use that name in the test and in nothing else.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomComposer.typeahead.test.tsx -t emoji`
Expected: FAIL — no emoji listbox, no "Insert emoji" button.

- [ ] **Step 3: Implement**

Imports: add `import { searchEmoji } from "./emojiData";` and `import { EmojiPicker, recordRecentEmoji } from "./EmojiPicker";`.

Beside `options`/`mentionOpen`, add:

```tsx
  const emojiOptions = trigger?.kind === "emoji" ? searchEmoji(trigger.query).slice(0, 8) : [];
  const emojiOpen = emojiOptions.length > 0;
  const listOpen = mentionOpen || emojiOpen;
```

Add the caret-aware inserter next to `choose`:

```tsx
  function insertAtCaret(value: string) {
    const start = field.current?.selectionStart ?? text.length;
    const end = field.current?.selectionEnd ?? start;
    const next = `${text.slice(0, start)}${value}${text.slice(end)}`;
    onTextChange(next);
    requestAnimationFrame(() => {
      field.current?.focus();
      field.current?.setSelectionRange(start + value.length, start + value.length);
    });
  }

  function chooseEmoji(emoji: string) {
    if (!trigger) return;
    const next = applyCompletion(text, trigger, emoji);
    recordRecentEmoji(emoji);
    onTextChange(next.text);
    setTrigger(null);
    requestAnimationFrame(() => {
      field.current?.focus();
      field.current?.setSelectionRange(next.caret, next.caret);
    });
  }
```

In the textarea's `onKeyDown`, change `if (mentionOpen) {` to `if (listOpen) {`, make the arrow branch use `const count = mentionOpen ? options.length : emojiOptions.length;` for the modulus, and make the Enter/Tab branch dispatch:

```tsx
            event.preventDefault();
            if (mentionOpen) choose(options[activeIndex] ?? options[0]);
            else chooseEmoji((emojiOptions[activeIndex] ?? emojiOptions[0]).emoji);
            return;
```

Change the textarea's `aria-expanded` to `listOpen`, `aria-controls` to `listOpen ? \`${fieldId}-targets\` : undefined`, and `aria-activedescendant` to `listOpen ? \`${fieldId}-option-${activeIndex}\` : undefined`.

Render the emoji list beside the mention list (same id, only one is ever open):

```tsx
    {emojiOpen && <div id={`${fieldId}-targets`} className="room-typeahead" role="listbox" aria-label="Insert an emoji">
      {emojiOptions.map((entry, index) => <button key={entry.emoji} id={`${fieldId}-option-${index}`} type="button" role="option"
        aria-selected={index === activeIndex} className="room-choice room-choice-button" data-active={index === activeIndex || undefined}
        onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => chooseEmoji(entry.emoji)}>
        <span aria-hidden="true" className="text-lg">{entry.emoji}</span><span className="flex-1 text-left truncate">{entry.name}</span>
      </button>)}
    </div>}
```

In `.room-composer-actions`, between the image button and the `@` button:

```tsx
      <EmojiPicker triggerLabel="Insert emoji" disabled={!editable} onPick={insertAtCaret} />
```

- [ ] **Step 4: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging/RoomComposer.tsx apps/frontend/src/components/messaging/__tests__/RoomComposer.typeahead.test.tsx
git commit -m "feat(messaging): emoji typeahead and caret-anchored emoji insert"
```

---

### Task 4: Formatting toolbar and `⌘B / ⌘I / ⌘⇧X / ⌘⇧C`

*Model: sonnet — eight controls plus a keymap over an existing keydown chain.*

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx`
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomComposer.format.test.tsx` (create)

**Interfaces:**
- Consumes: `wrapSelection`, `MarkdownStyle` (Task 1).
- Produces: a `role="toolbar"` `aria-label="Formatting"` row above the textarea with buttons labelled exactly `Bold`, `Italic`, `Strikethrough`, `Bulleted list`, `Numbered list`, `Inline code`, `Code block`, `Link`.

**Shortcuts (spec):** `⌘B`/`Ctrl+B` bold · `⌘I`/`Ctrl+I` italic · `⌘⇧X` strikethrough · `⌘⇧C` code block. They fire only when no typeahead list is open.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/frontend/src/components/messaging/__tests__/RoomComposer.format.test.tsx
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MemberRef } from "@lightbulb/shared";
import { RoomComposer } from "../RoomComposer";

function Harness() {
  const [text, setText] = useState("ship it");
  const [mentions, setMentions] = useState<MemberRef[]>([]);
  return <RoomComposer text={text} onTextChange={setText} members={[]} selectedMentions={mentions}
    onMentionsChange={setMentions} clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={vi.fn()} />;
}

const field = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
const select = (start: number, end: number) => { field().setSelectionRange(start, end); };

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("exposes the eight formatting controls in one toolbar", () => {
  render(<Harness />);
  const toolbar = screen.getByRole("toolbar", { name: "Formatting" });
  for (const name of ["Bold", "Italic", "Strikethrough", "Bulleted list", "Numbered list", "Inline code", "Code block", "Link"]) {
    expect(screen.getByRole("button", { name })).toBe(toolbar.querySelector(`[aria-label="${name}"]`));
  }
});

it("wraps the selection from the toolbar and unwraps on a second press", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  expect(field()).toHaveValue("**ship** it");
  select(2, 6);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  expect(field()).toHaveValue("ship it");
});

it("wraps from the keyboard shortcuts", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.keyDown(field(), { key: "b", metaKey: true });
  expect(field()).toHaveValue("**ship** it");
});

it("uses shift+X for strikethrough and shift+C for a code block", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.keyDown(field(), { key: "X", metaKey: true, shiftKey: true });
  expect(field()).toHaveValue("~~ship~~ it");
  select(2, 6);
  fireEvent.keyDown(field(), { key: "C", metaKey: true, shiftKey: true });
  expect(field()).toHaveValue("~~```\nship\n```~~ it");
});

it("builds a list from the touched lines", () => {
  render(<Harness />);
  fireEvent.change(field(), { target: { value: "one\ntwo", selectionStart: 7 } });
  select(0, 7);
  fireEvent.click(screen.getByRole("button", { name: "Bulleted list" }));
  expect(field()).toHaveValue("- one\n- two");
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomComposer.format.test.tsx`
Expected: FAIL — no `toolbar` role.

- [ ] **Step 3: Implement**

Imports: `import { wrapSelection, type MarkdownStyle } from "./composerText";` and extend the lucide import to `import { AtSign, Bold, Code, Italic, ImagePlus, Link2, List, ListOrdered, Send, SquareCode, Strikethrough, X } from "lucide-react";`.

Add above the `return`:

```tsx
  const FORMAT_CONTROLS: { style: MarkdownStyle; label: string; Icon: typeof Bold }[] = [
    { style: "bold", label: "Bold", Icon: Bold },
    { style: "italic", label: "Italic", Icon: Italic },
    { style: "strike", label: "Strikethrough", Icon: Strikethrough },
    { style: "ul", label: "Bulleted list", Icon: List },
    { style: "ol", label: "Numbered list", Icon: ListOrdered },
    { style: "code", label: "Inline code", Icon: Code },
    { style: "codeblock", label: "Code block", Icon: SquareCode },
    { style: "link", label: "Link", Icon: Link2 },
  ];

  function format(style: MarkdownStyle) {
    const start = field.current?.selectionStart ?? text.length;
    const end = field.current?.selectionEnd ?? start;
    const next = wrapSelection(text, start, end, style);
    onTextChange(next.text);
    requestAnimationFrame(() => {
      field.current?.focus();
      field.current?.setSelectionRange(next.selectionStart, next.selectionEnd);
    });
  }
```

In the textarea's `onKeyDown`, immediately **after** the `if (listOpen) { … }` block and **before** the Enter-to-send branch:

```tsx
        if (event.metaKey || event.ctrlKey) {
          const key = event.key.toLowerCase();
          const style: MarkdownStyle | null = event.shiftKey
            ? key === "x" ? "strike" : key === "c" ? "codeblock" : null
            : key === "b" ? "bold" : key === "i" ? "italic" : null;
          if (style) { event.preventDefault(); format(style); return; }
        }
```

Render the toolbar as the first element inside the form, above the `sr-only` label:

```tsx
    <div className="room-format" role="toolbar" aria-label="Formatting">
      {FORMAT_CONTROLS.map(({ style, label, Icon }) => <Button key={style} type="button" variant="ghost" size="icon-sm"
        aria-label={label} title={label} disabled={!editable} onClick={() => format(style)}><Icon aria-hidden="true" /></Button>)}
    </div>
```

CSS, appended to `messaging.css`:

```css
.room-format { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; padding-bottom: 4px; border-bottom: 1px solid var(--border); }
```

- [ ] **Step 4: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging/RoomComposer.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/RoomComposer.format.test.tsx
git commit -m "feat(messaging): markdown formatting toolbar and shortcuts"
```

---

### Task 5: `↑` in an empty composer edits your last message

*Model: sonnet — four files, and the edit state currently lives inside `RoomMessageContent`.*

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomMessageItem.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomMessageContent.tsx`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomComposer.editLast.test.tsx` (create)
- Test: `apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx` (append)

**Interfaces:**
- Produces:
  - `RoomComposerProps.onEditLast?: () => void` — fired on `ArrowUp` when the field is empty, no typeahead list is open, and the composer is editable.
  - `RoomMessageItemProps.startEditing?: boolean` → `RoomMessageContent` prop `startEditing?: boolean`.
  - `RoomPanel` holds `const [editTarget, setEditTarget] = useState<string | null>(null)` and passes `startEditing={editTarget === message.messageId}`.

**Why it is not shipped:** the spec credits `↑`-to-edit to #2473, but #2473 shipped edit/delete only through `RoomMessageContent`'s own dropdown; grep for `ArrowUp` in `components/messaging/` returns nothing.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/frontend/src/components/messaging/__tests__/RoomComposer.editLast.test.tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";

const base: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [], selectedMentions: [], onMentionsChange: vi.fn(),
  clientMessageId: "11111111-1111-4111-8111-111111111111", sendState: "idle", onSubmit: vi.fn(),
};
beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("asks to edit the last message when ArrowUp lands in an empty field", () => {
  const onEditLast = vi.fn();
  render(<RoomComposer {...base} onEditLast={onEditLast} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(onEditLast).toHaveBeenCalledOnce();
});

it("leaves ArrowUp alone once the field has text", () => {
  const onEditLast = vi.fn();
  render(<RoomComposer {...base} text="draft" onEditLast={onEditLast} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(onEditLast).not.toHaveBeenCalled();
});
```

```tsx
// append to apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx
it("opens the edit form for your last message when ArrowUp lands in an empty composer", () => {
  render(<RoomPanel {...props} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(screen.getByRole("textbox", { name: "Edit message" })).toBeInTheDocument();
});
```

> `props` is the existing `RoomPanel.test.tsx` fixture. Before writing this case, read that file's fixture and make sure it contains at least one non-deleted message authored by `{ kind: "human", id: props.currentUserId }` and supplies `onUpdateMessage`; if it does not, add such a message to a local `messages` array for this case only — do not mutate the shared fixture.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomComposer.editLast.test.tsx src/components/messaging/__tests__/RoomPanel.test.tsx -t ArrowUp`
Expected: FAIL.

- [ ] **Step 3: Implement**

`RoomComposer.tsx` — add `onEditLast?: () => void;` to `RoomComposerProps`, destructure it, and insert this branch in `onKeyDown` **after** the `listOpen` block and **before** the meta-key block:

```tsx
        if (event.key === "ArrowUp" && !text && onEditLast) {
          event.preventDefault();
          onEditLast();
          return;
        }
```

`RoomMessageContent.tsx` — add `startEditing` to the props type and open the form when it flips true, using the render-phase adjust pattern already used in `RoomComposer` (`if (previousState !== sendState)`):

```tsx
export function RoomMessageContent({ message, currentUserId, actionable, onUpdate, startEditing = false, actions, children }: {
  message: CanonicalRoomMessage;
  currentUserId: string;
  actionable: boolean;
  onUpdate?: (messageId: string, request: UpdateMessageRequest) => Promise<void>;
  startEditing?: boolean;
  children: ReactNode;
  actions?: ReactNode;
}) {
```

and, directly after the `ownMessage` const:

```tsx
  const [editRequested, setEditRequested] = useState(startEditing);
  if (editRequested !== startEditing) {
    setEditRequested(startEditing);
    if (startEditing && ownMessage && onUpdate && request === null) {
      setRequest({ operationId: crypto.randomUUID(), expectedRevision: message.messageRevision ?? 0, text: message.text });
    }
  }
```

`RoomMessageItem.tsx` — add `startEditing?: boolean;` to `RoomMessageItemProps`, destructure it, and pass `startEditing={startEditing}` to `<RoomMessageContent …>`.

`RoomPanel.tsx` — in `RoomContent`:

```tsx
  const [editTarget, setEditTarget] = useState<string | null>(null);
```
pass `startEditing={editTarget === message.messageId}` in `renderMessage`'s `<RoomMessageItem …>`, and give the main composer the callback:

```tsx
      <RoomComposer {...composer}
        onSubmit={markSent(composer.onSubmit)}
        onRetry={composer.onRetry ? markSent(composer.onRetry) : undefined}
        onEditLast={onUpdateMessage ? () => {
          const own = mainMessages.filter((message) => message.author.kind === "human" && message.author.id === currentUserId && !message.deletedAt);
          setEditTarget(own.length ? own[own.length - 1].messageId : null);
        } : undefined}
        disabled={!actionable || composer.disabled} />
```

The target is cleared by the edit form itself: `RoomMessageContent` restores focus and clears `request` on save or cancel, and a second `ArrowUp` re-sets the same id (the render-phase guard only fires on a *change*, so also clear it when the message list changes identity — add `onSelectThread`-style cleanup by resetting in the same callback when `own.length === 0`, which the code above already does).

- [ ] **Step 4: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging/RoomComposer.tsx apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/RoomMessageItem.tsx apps/frontend/src/components/messaging/RoomMessageContent.tsx apps/frontend/src/components/messaging/__tests__
git commit -m "feat(messaging): up arrow edits your last message"
```

---

### Task 6: Drag-and-drop images onto the composer and the message list

*Model: haiku — two drop handlers reusing the shipped `addImages` path.*

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx`
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomComposer.dnd.test.tsx` (create)

**Interfaces:**
- Produces: `RoomComposerProps.dropTargetRef?: React.RefObject<HTMLElement | null>` — an extra element (the message list) whose drops route into the same composer upload path. The composer form itself is always a drop target.

**Behaviour:** dragging image files over the composer (or the message list) sets `data-dropping` on the composer and shows the copy `Drop images to attach`; dropping calls the existing `addImages`, which already caps at 4 and uploads via `onUploadAttachment`. Non-image drags are ignored. This reuses the shipped paste path verbatim — no second upload code.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/messaging/__tests__/RoomComposer.dnd.test.tsx
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ImageAttachment, MemberRef } from "@lightbulb/shared";
import { RoomComposer } from "../RoomComposer";

const attachment: ImageAttachment = { id: "att-1", fileName: "shot.png", contentType: "image/png", size: 8 };

function Harness({ onUploadAttachment }: { onUploadAttachment: (file: File) => Promise<ImageAttachment> }) {
  const [text, setText] = useState("");
  const [mentions, setMentions] = useState<MemberRef[]>([]);
  return <RoomComposer text={text} onTextChange={setText} members={[]} selectedMentions={mentions}
    onMentionsChange={setMentions} clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle"
    onSubmit={vi.fn()} onUploadAttachment={onUploadAttachment} />;
}

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("marks the composer while an image drag is over it and uploads on drop", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  const form = screen.getByRole("form", { name: "Message composer" });
  const file = new File(["x"], "shot.png", { type: "image/png" });
  fireEvent.dragOver(form, { dataTransfer: { types: ["Files"], files: [file] } });
  expect(form).toHaveAttribute("data-dropping", "true");
  fireEvent.drop(form, { dataTransfer: { types: ["Files"], files: [file] } });
  await waitFor(() => expect(upload).toHaveBeenCalledWith(file));
  expect(form).not.toHaveAttribute("data-dropping");
});

it("ignores a drag that carries no files", () => {
  render(<Harness onUploadAttachment={vi.fn(async () => attachment)} />);
  const form = screen.getByRole("form", { name: "Message composer" });
  fireEvent.dragOver(form, { dataTransfer: { types: ["text/plain"], files: [] } });
  expect(form).not.toHaveAttribute("data-dropping");
});
```

> The composer's `<form>` already carries `aria-label={`${label} composer`}`, so `getByRole("form", { name: "Message composer" })` resolves with the default `label = "Message"`. Confirm `ImageAttachment`'s fields with `grep -n -A 8 "interface ImageAttachment" packages/shared/src/messaging/types.ts` and match the fixture to it.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomComposer.dnd.test.tsx`
Expected: FAIL — no `data-dropping` attribute.

- [ ] **Step 3: Implement**

`RoomComposer.tsx` — add `dropTargetRef?: RefObject<HTMLElement | null>;` to the props (import `type RefObject` from react), add `const [dropping, setDropping] = useState(false);`, and:

```tsx
  const carriesFiles = (transfer: DataTransfer | null) => !!transfer && [...transfer.types].includes("Files");

  function dropImages(transfer: DataTransfer | null) {
    setDropping(false);
    const files = [...(transfer?.files ?? [])].filter((file) => file.type.startsWith("image/"));
    if (files.length) addImages(files);
  }

  useEffect(() => {
    const node = dropTargetRef?.current;
    if (!node || !onUploadAttachment) return;
    const over = (event: DragEvent) => { if (carriesFiles(event.dataTransfer)) { event.preventDefault(); setDropping(true); } };
    const leave = () => setDropping(false);
    const drop = (event: DragEvent) => { if (!carriesFiles(event.dataTransfer)) return; event.preventDefault(); dropImages(event.dataTransfer); };
    node.addEventListener("dragover", over);
    node.addEventListener("dragleave", leave);
    node.addEventListener("drop", drop);
    return () => { node.removeEventListener("dragover", over); node.removeEventListener("dragleave", leave); node.removeEventListener("drop", drop); };
  });
```

On the `<form>`:

```tsx
    data-dropping={dropping || undefined}
    onDragOver={(event) => { if (carriesFiles(event.dataTransfer)) { event.preventDefault(); setDropping(true); } }}
    onDragLeave={() => setDropping(false)}
    onDrop={(event) => { if (!carriesFiles(event.dataTransfer)) return; event.preventDefault(); dropImages(event.dataTransfer); }}
```

and render the hint just below the toolbar:

```tsx
    {dropping && <p className="room-drop-hint text-sm text-muted-foreground" role="status">Drop images to attach</p>}
```

`RoomPanel.tsx` — add `const historyRef = useRef<HTMLDivElement>(null);`, wrap the `<RoomTimeline …>` in `<div ref={historyRef} className="contents">`, and pass `dropTargetRef={historyRef}` to the main `<RoomComposer …>`.

CSS:

```css
.room-composer[data-dropping] { border-color: var(--ring); border-style: dashed; }
.room-drop-hint { padding: 0 4px; }
```

- [ ] **Step 4: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging/RoomComposer.tsx apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/RoomComposer.dnd.test.tsx
git commit -m "feat(messaging): drag and drop images onto the composer and the message list"
```

---

# PR B — drafts, the respond hint, and the keyboard map (Tasks 7–10)

### Task 7: Per-room and per-thread drafts, the Drafts sidebar row, and the who-will-respond hint

*Model: sonnet — storage, context lifecycle, a sidebar section, and a dispatch mirror in one deliverable.*

**Files:**
- Create: `apps/frontend/src/components/messaging/drafts.ts`
- Create: `apps/frontend/src/components/messaging/respondHint.ts`
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomDirectory.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx`
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md` and `apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md`
- Test: `apps/frontend/src/components/messaging/__tests__/drafts.test.ts` (create)
- Test: `apps/frontend/src/components/messaging/__tests__/respondHint.test.ts` (create)
- Test: `apps/frontend/src/components/messaging/__tests__/RoomDirectory.test.tsx` (append)
- Test: `apps/frontend/src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx` (append)

**Interfaces:**
- Produces:
  ```ts
  // drafts.ts
  export interface StoredDraft { text: string; mentions: MemberRef[] }
  export function draftKey(ownerScopeId: string, roomId: string, threadId: string | null): string;
  export function readDraft(key: string): StoredDraft;
  export function writeDraft(key: string, draft: StoredDraft): void;  // removes the row when empty
  export function listDraftRoomIds(ownerScopeId: string): string[];

  // respondHint.ts
  export interface RespondTarget { kind: "human" | "agent"; id: string; displayName: string; listenMode: ListenMode | null }
  export function respondHint(roomKind: RoomKind | undefined, members: readonly RespondTarget[], mentions: readonly MemberRef[], runningAgentIds: readonly string[]): string;
  ```
  - `RoomDirectoryProps.draftRoomIds?: readonly string[]`.
  - `RoomComposerProps.roomKind?: RoomKind` and `RoomComposerProps.runningAgentIds?: readonly string[]`.
- Consumes: `resolveTypedMentions` (Task 1) — the hint reads the same effective `mentions` the send path builds, which is why it can never promise a reply the dispatcher will not make.

**Storage keys:** `lightbulb.messaging.draft|<ownerScopeId>|<roomId>|<threadId or empty>`. `|` is the separator because Clerk ids and room ids may contain `_` and `-` but never `|`.

**Hint rules (mirror of `select_dispatch_targets`, `apps/backend/core/services/messaging.py:1072`):** in order — `observe` → `<Name> observing` (never runs, in any room kind, even when mentioned) · a live run → `<Name> is working — sending now steers that run` · DM, or mentioned, or `active` → `<Name> will reply` · otherwise `mentions` mode → `<Name> when mentioned`. Joined with ` · `. A group DM is **not** a DM to the dispatcher. No agents → empty string, and nothing renders.

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/components/messaging/__tests__/drafts.test.ts
import { afterEach, describe, expect, it } from "vitest";
import { draftKey, listDraftRoomIds, readDraft, writeDraft } from "../drafts";

afterEach(() => localStorage.clear());

describe("drafts", () => {
  it("round-trips text and mentions for a room and for a thread separately", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "room draft", mentions: [{ kind: "agent", id: "agent-friday" }] });
    writeDraft(draftKey("org_1", "room-a", "msg-1"), { text: "thread draft", mentions: [] });
    expect(readDraft(draftKey("org_1", "room-a", null))).toEqual({ text: "room draft", mentions: [{ kind: "agent", id: "agent-friday" }] });
    expect(readDraft(draftKey("org_1", "room-a", "msg-1"))).toEqual({ text: "thread draft", mentions: [] });
  });
  it("scopes drafts to the owner", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "mine", mentions: [] });
    expect(readDraft(draftKey("org_2", "room-a", null))).toEqual({ text: "", mentions: [] });
    expect(listDraftRoomIds("org_2")).toEqual([]);
  });
  it("removes the row when the draft empties, and drops the room from the list", () => {
    const key = draftKey("org_1", "room-a", null);
    writeDraft(key, { text: "typing", mentions: [] });
    expect(listDraftRoomIds("org_1")).toEqual(["room-a"]);
    writeDraft(key, { text: "   ", mentions: [] });
    expect(localStorage.getItem(key)).toBeNull();
    expect(listDraftRoomIds("org_1")).toEqual([]);
  });
  it("lists a room once even when it also has a thread draft, and survives junk", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "a", mentions: [] });
    writeDraft(draftKey("org_1", "room-a", "msg-1"), { text: "b", mentions: [] });
    localStorage.setItem("lightbulb.messaging.draft|org_1|room-b|", "not json");
    expect(listDraftRoomIds("org_1")).toEqual(["room-a"]);
  });
  it("reads an absent or corrupt row as an empty draft", () => {
    expect(readDraft(draftKey("org_1", "nope", null))).toEqual({ text: "", mentions: [] });
    localStorage.setItem(draftKey("org_1", "bad", null), "{");
    expect(readDraft(draftKey("org_1", "bad", null))).toEqual({ text: "", mentions: [] });
  });
});
```

```ts
// apps/frontend/src/components/messaging/__tests__/respondHint.test.ts
import { describe, expect, it } from "vitest";
import { respondHint, type RespondTarget } from "../respondHint";

const friday: RespondTarget = { kind: "agent", id: "agent-friday", displayName: "Friday", listenMode: "active" };
const gauge: RespondTarget = { kind: "agent", id: "agent-gauge", displayName: "Gauge", listenMode: "mentions" };
const atlas: RespondTarget = { kind: "agent", id: "agent-atlas", displayName: "Atlas", listenMode: "observe" };
const person: RespondTarget = { kind: "human", id: "user-aden", displayName: "Aden Kim", listenMode: null };

describe("respondHint", () => {
  it("renders the compact channel form", () => {
    expect(respondHint("channel", [person, friday, gauge, atlas], [], []))
      .toBe("Friday will reply · Gauge when mentioned · Atlas observing");
  });
  it("promotes a mentions agent the moment the draft mentions it", () => {
    expect(respondHint("channel", [gauge], [{ kind: "agent", id: "agent-gauge" }], [])).toBe("Gauge will reply");
  });
  it("treats a group DM as a channel, not a DM", () => {
    expect(respondHint("group-dm", [gauge], [], [])).toBe("Gauge when mentioned");
  });
  it("makes every agent reply in a DM regardless of listen mode", () => {
    expect(respondHint("dm", [gauge], [], [])).toBe("Gauge will reply");
  });
  it("never promises a reply from an observe agent, even mentioned in a DM", () => {
    expect(respondHint("dm", [atlas], [{ kind: "agent", id: "agent-atlas" }], [])).toBe("Atlas observing");
  });
  it("says a live run will be steered", () => {
    expect(respondHint("channel", [friday], [], ["agent-friday"])).toBe("Friday is working — sending now steers that run");
  });
  it("is empty when the room has no agents", () => {
    expect(respondHint("channel", [person], [], [])).toBe("");
  });
});
```

```tsx
// append to apps/frontend/src/components/messaging/__tests__/RoomDirectory.test.tsx
// `base` is the module-level DirectoryRoom fixture already in this file
// (roomId "channel", kind "channel", name "Launch").
const directory = { ownerScopeId: "org", state: "ready" as const, rooms: [base], participants: [atlas], onSelectRoom: vi.fn(), onSelectParticipant: vi.fn() };

it("lists rooms with a saved draft under Drafts and marks the row", () => {
  render(<RoomDirectory {...directory} draftRoomIds={["channel"]} />);
  const drafts = screen.getByRole("region", { name: "Drafts" });
  expect(within(drafts).getByRole("button", { name: /Launch/ })).toBeInTheDocument();
  expect(screen.getAllByLabelText("Unsent draft").length).toBeGreaterThan(0);
});

it("renders no Drafts section when nothing is drafted", () => {
  render(<RoomDirectory {...directory} draftRoomIds={[]} />);
  expect(screen.queryByRole("region", { name: "Drafts" })).not.toBeInTheDocument();
});
```

```tsx
// append to apps/frontend/src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx
it("restores a per-room draft when the owner returns to the room", async () => {
  // Read this file's existing harness first: it already renders the provider
  // with a mocked useRooms. Drive selectRoom to room B and back to room A,
  // typing in A first, and assert the composer prop `text` comes back.
});
```

> Replace that placeholder comment with a real case shaped to the harness already in `MessagingWorkspaceContext.test.tsx` — read the file, reuse its mock of `@/hooks/useRooms`, type into the composer via `directoryProps`/`roomProps`, switch `activeRoomId`, switch back, and assert `roomProps.composer.text`. The behaviour under test is fixed: **type in room A, open room B, return to A, the text is back**.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/drafts.test.ts src/components/messaging/__tests__/respondHint.test.ts src/components/messaging/__tests__/RoomDirectory.test.tsx`
Expected: FAIL — missing modules and no `Drafts` region.

- [ ] **Step 3: Write `drafts.ts`**

```ts
// apps/frontend/src/components/messaging/drafts.ts
import type { MemberRef } from "@lightbulb/shared";

const PREFIX = "lightbulb.messaging.draft|";

export interface StoredDraft {
  text: string;
  mentions: MemberRef[];
}

const EMPTY: StoredDraft = { text: "", mentions: [] };

/** `lightbulb.messaging.draft|<owner>|<room>|<thread or empty>` — `|` appears in no Clerk or room id. */
export function draftKey(ownerScopeId: string, roomId: string, threadId: string | null): string {
  return `${PREFIX}${ownerScopeId}|${roomId}|${threadId ?? ""}`;
}

export function readDraft(key: string): StoredDraft {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return { ...EMPTY };
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return { ...EMPTY };
    const record = parsed as Partial<StoredDraft>;
    return {
      text: typeof record.text === "string" ? record.text : "",
      mentions: Array.isArray(record.mentions)
        ? record.mentions.filter((ref): ref is MemberRef =>
            !!ref && (ref.kind === "human" || ref.kind === "agent") && typeof ref.id === "string")
        : [],
    };
  } catch {
    return { ...EMPTY };
  }
}

export function writeDraft(key: string, draft: StoredDraft): void {
  try {
    if (!draft.text.trim() && draft.mentions.length === 0) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(draft));
  } catch {
    /* per-viewer convenience only */
  }
}

export function clearDraft(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* per-viewer convenience only */
  }
}

/** Room ids with a non-empty draft for this owner, in insertion order, de-duplicated. */
export function listDraftRoomIds(ownerScopeId: string): string[] {
  const head = `${PREFIX}${ownerScopeId}|`;
  const rooms: string[] = [];
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (!key?.startsWith(head)) continue;
      const roomId = key.slice(head.length).split("|")[0];
      const draft = readDraft(key);
      if (!roomId || (!draft.text.trim() && draft.mentions.length === 0)) continue;
      if (!rooms.includes(roomId)) rooms.push(roomId);
    }
  } catch {
    return [];
  }
  return rooms;
}
```

- [ ] **Step 4: Write `respondHint.ts`**

```ts
// apps/frontend/src/components/messaging/respondHint.ts
import type { ListenMode, MemberRef, RoomKind } from "@lightbulb/shared";

export interface RespondTarget {
  kind: "human" | "agent";
  id: string;
  displayName: string;
  listenMode: ListenMode | null;
}

/**
 * Mirrors core/services/messaging.py select_dispatch_targets. Derived ONLY
 * from room kind, listen modes, the draft's structured mentions, and live run
 * state, so it can never disagree with what the backend will actually do.
 */
export function respondHint(
  roomKind: RoomKind | undefined,
  members: readonly RespondTarget[],
  mentions: readonly MemberRef[],
  runningAgentIds: readonly string[],
): string {
  const mentioned = new Set(mentions.filter((ref) => ref.kind === "agent").map((ref) => ref.id));
  const parts: string[] = [];
  for (const member of members) {
    if (member.kind !== "agent") continue;
    // observe never runs — not on ambient traffic, not when mentioned, not in a DM.
    if (member.listenMode === "observe") parts.push(`${member.displayName} observing`);
    else if (runningAgentIds.includes(member.id)) parts.push(`${member.displayName} is working — sending now steers that run`);
    else if (roomKind === "dm" || mentioned.has(member.id) || member.listenMode === "active") parts.push(`${member.displayName} will reply`);
    else parts.push(`${member.displayName} when mentioned`);
  }
  return parts.join(" · ");
}
```

- [ ] **Step 5: Wire drafts into `MessagingWorkspaceContext.tsx`**

Import `import { clearDraft, draftKey, listDraftRoomIds, readDraft, writeDraft } from "./drafts";`.

Add above the existing reset block:

```tsx
  const scope = orgId ?? userId ?? "signed-out";
  const [draftThreadId, setDraftThreadId] = useState<string | null>(null);
```

Replace the whole `if (draftRoomId !== rooms.activeRoomId) { … }` block with:

```tsx
  // Reset before the new room's composer consumes a pending draft/focus
  // handoff — restoring, not clearing, so a half-typed message survives a
  // room switch (spec: per-room drafts).
  if (draftRoomId !== rooms.activeRoomId) {
    setDraftRoomId(rooms.activeRoomId);
    const restored = rooms.activeRoomId ? readDraft(draftKey(scope, rooms.activeRoomId, null)) : { text: "", mentions: [] };
    setDraft(restored.text);
    setMentions(restored.mentions);
    setClientMessageId(crypto.randomUUID());
    setThreadId(null);
    setDraftThreadId(null);
    setThreadDraft("");
    setThreadMentions([]);
    setThreadClientMessageId(crypto.randomUUID());
  } else if (draftThreadId !== threadId) {
    setDraftThreadId(threadId);
    const restored = rooms.activeRoomId && threadId ? readDraft(draftKey(scope, rooms.activeRoomId, threadId)) : { text: "", mentions: [] };
    setThreadDraft(restored.text);
    setThreadMentions(restored.mentions);
    setThreadClientMessageId(crypto.randomUUID());
  }
```

Add the persistence effects after the typing effect:

```tsx
  useEffect(() => {
    if (!rooms.activeRoomId) return;
    writeDraft(draftKey(scope, rooms.activeRoomId, null), { text: draft, mentions });
  }, [scope, rooms.activeRoomId, draft, mentions]);

  useEffect(() => {
    if (!rooms.activeRoomId || !threadId) return;
    writeDraft(draftKey(scope, rooms.activeRoomId, threadId), { text: threadDraft, mentions: threadMentions });
  }, [scope, rooms.activeRoomId, threadId, threadDraft, threadMentions]);
```

In `send`, drop the stored draft once the message is away — add `clearDraft(draftKey(scope, request.roomId ?? rooms.activeRoomId ?? "", thread ? request.threadRootMessageId : null));` **no**: `CreateMessageRequest` has no `roomId`. Use the active room:

```tsx
  const send = useCallback(async (request: CreateMessageRequest, thread = false) => {
    await sendRoomMessage(request);
    const roomId = rooms.activeRoomId;
    if (thread) {
      if (threadClientMessageIdRef.current !== request.clientMessageId) return;
      if (roomId && request.threadRootMessageId) clearDraft(draftKey(scope, roomId, request.threadRootMessageId));
      setThreadDraft("");
      setThreadMentions([]);
      setThreadClientMessageId(crypto.randomUUID());
    } else {
      if (clientMessageIdRef.current !== request.clientMessageId) return;
      if (roomId) clearDraft(draftKey(scope, roomId, null));
      setDraft("");
      setMentions([]);
      setClientMessageId(crypto.randomUUID());
    }
  }, [sendRoomMessage, rooms.activeRoomId, scope]);
```

Add the directory input (recomputed on every keystroke — `localStorage.key()` over a few dozen entries is cheaper than a second source of truth):

```tsx
  const draftRoomIds = useMemo(() => listDraftRoomIds(scope), [scope, draft, mentions, threadDraft, threadMentions]);
```
and pass `draftRoomIds,` in `directoryProps`.

Give the composers the hint inputs — in the `composer` object add:

```tsx
    roomKind: rooms.activeRoom?.kind,
    runningAgentIds: rooms.runs.filter((run) => run.state === "running").map((run) => run.agentId),
```
and the same two lines in `threadComposer.composer`.

- [ ] **Step 6: Render the hint in `RoomComposer.tsx`**

Import `import { respondHint } from "./respondHint";`, add `roomKind?: RoomKind;` and `runningAgentIds?: readonly string[];` to `RoomComposerProps` (import `type RoomKind` from `@lightbulb/shared`), destructure with `runningAgentIds = []`, then above the `return`:

```tsx
  const hint = respondHint(roomKind, members, mentions, runningAgentIds);
```
and render it directly above the help line:

```tsx
    {hint && <p className="room-respond-hint text-sm text-muted-foreground">{hint}</p>}
```

CSS:

```css
.room-respond-hint { padding: 0 4px; font-size: .75rem; }
```

- [ ] **Step 7: Add the Drafts section to `RoomDirectory.tsx`**

Add `draftRoomIds?: readonly string[];` to `RoomDirectoryProps`, destructure with `draftRoomIds = []`, extend the lucide import with `Pencil`, and render the section immediately after the `["Channels", "Direct messages"]` map:

```tsx
    {draftRoomIds.length > 0 && <section aria-label="Drafts">
      <h3 className="room-directory-group">Drafts</h3>
      <ul>{rooms.filter((room) => draftRoomIds.includes(room.roomId)).map((room) => {
        const label = roomDisplayName(room, room.participants);
        return <li key={`draft:${room.roomId}`} className="room-directory-row">
          <button type="button" className="room-directory-entry" aria-current={room.roomId === currentRoomId ? "page" : undefined}
            onClick={() => onSelectRoom(room.roomId)}>
            <Pencil size={16} aria-label="Unsent draft" />
            <span className="min-w-0 flex-1"><span className="block truncate" title={label}>{label}</span></span>
          </button>
        </li>;
      })}</ul>
    </section>}
```

and mark the ordinary rows: inside the existing room `<button>`, after the unread count, add

```tsx
            {draftRoomIds.includes(room.roomId) && <Pencil size={14} aria-label="Unsent draft" className="text-muted-foreground" />}
```

- [ ] **Step 8: Update the agent-facing surfaces doc**

In `apps/infra/openclaw/skills/lightbulb/references/surfaces.md`, replace the **Chat** row's description with:

> Talk to any agent; upload files (drag images onto the composer or the message list); browse the workspace. The composer is a markdown field with a formatting toolbar (`⌘B` bold, `⌘I` italic, `⌘⇧X` strikethrough, `⌘⇧C` code block), an inline `@` typeahead over room members and a `:` emoji typeahead, and `↑` on an empty field to edit your last message. A typed `@handle` or `@Display Name` that matches a room member notifies them exactly as picking them from the list does; one that matches nobody stays plain text and notifies nobody. A line under the field says which agents will reply before you send ("Friday will reply · Gauge when mentioned · Atlas observing") — and if an agent is mid-run it reads "is working — sending now steers that run". Unsent text is kept per room and per thread on that browser and the sidebar lists those rooms under **Drafts**.

In `apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md`, add under the `Message actions` anchor block:

```
  - Composer .............. references/surfaces.md
                              <- apps/frontend/src/components/messaging/RoomComposer.tsx (typeaheads, formatting toolbar,
                                 drag-and-drop, up-arrow edit); composerText.ts (caret grammar); mentions.ts
                                 resolveTypedMentions (typed tokens resolve on send); respondHint.ts (must mirror
                                 core/services/messaging.py select_dispatch_targets); drafts.ts (lightbulb.messaging.draft| keys)
```

- [ ] **Step 9: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging apps/infra/openclaw/skills/lightbulb/references/surfaces.md apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md
git commit -m "feat(messaging): per-room drafts, Drafts row, and the who-will-respond hint"
```

---

### Task 8: `⌘K` reaches every conversation

*Model: sonnet — crosses into the Teams palette; needs the optional-context degrade to be right.*

**Files:**
- Create: `apps/frontend/src/components/messaging/roomCommands.ts`
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx`
- Modify: `apps/frontend/src/components/teams/command-palette/CommandPalette.tsx`
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md`
- Test: `apps/frontend/src/components/messaging/__tests__/roomCommands.test.tsx` (create)

**Interfaces:**
- Produces:
  ```ts
  // MessagingWorkspaceContext.tsx
  export function useMessagingWorkspaceOptional(): MessagingWorkspaceValue | null;
  // roomCommands.ts
  export interface RoomCommandItem { id: string; label: string; sublabel: string; run: () => void }
  export function useRoomCommandResults(query: string, enabled: boolean): RoomCommandItem[];
  ```
  `CommandPalette`'s `FlatItem` gains `run?: () => void`; `select` prefers `item.run` over `window.history.pushState(null, "", item.path)`.
- Consumes: `directoryProps.rooms` / `directoryProps.participants` / `directoryProps.onSelectParticipant` / `openMessage` (all already on the context value), `activateMessagesPanel` from `@/components/workspace/workspaceController`, `roomDisplayName` from `./types`.

**Why reuse (Open decision 3):** `CommandPaletteProvider` already owns the global `⌘K` and is mounted at `WorkspaceShell.tsx:449`, inside `DesktopMessagingProvider` (line 448) — so a palette group can read the messaging context. It is **also** mounted in `InlineTeamsPanel.tsx:63`, which is *not* inside a messaging provider, which is exactly why `useRoomCommandResults` reads the context optionally and returns `[]` there instead of throwing.

**Ordering:** the Conversations group renders **first**, so `⌘K` + one character + `Enter` (three keystrokes) opens a matching room — the spec's acceptance bar.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/messaging/__tests__/roomCommands.test.tsx
import { renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useRoomCommandResults } from "../roomCommands";

vi.mock("@/components/workspace/workspaceController", () => ({ activateMessagesPanel: vi.fn() }));

it("returns nothing when no messaging provider is mounted", () => {
  const { result } = renderHook(() => useRoomCommandResults("", true));
  expect(result.current).toEqual([]);
});

it("returns nothing when the palette is closed", () => {
  const { result } = renderHook(() => useRoomCommandResults("road", false));
  expect(result.current).toEqual([]);
});
```

> Then add a provider-backed case: wrap `renderHook` in a `MessagingWorkspaceContext` provider stub (import `useMessagingWorkspaceOptional`'s context via a small local `MessagingWorkspaceProvider` mock is not possible — instead `vi.mock("../MessagingWorkspaceContext", …)` returning a fixed `useMessagingWorkspaceOptional`) and assert that (a) querying `"road"` yields `{ label: "roadmap", sublabel: "Channel" }` first, (b) `run()` calls `openMessage(roomId, null)` then `activateMessagesPanel()`, and (c) a participant row is labelled `Message Friday` with sublabel `Agent`. Write those three assertions out before implementing.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/roomCommands.test.tsx`
Expected: FAIL — `Cannot find module '../roomCommands'`.

- [ ] **Step 3: Export the optional context hook**

In `MessagingWorkspaceContext.tsx`, beside `useMessagingWorkspace`:

```tsx
/** Null outside a MessagingWorkspaceProvider — the Teams command palette is
 *  also mounted in InlineTeamsPanel, where no messaging provider exists. */
export function useMessagingWorkspaceOptional(): MessagingWorkspaceValue | null {
  return useContext(MessagingWorkspaceContext);
}
```

- [ ] **Step 4: Write `roomCommands.ts`**

```ts
// apps/frontend/src/components/messaging/roomCommands.ts
"use client";

import { useMemo } from "react";
import { activateMessagesPanel } from "@/components/workspace/workspaceController";
import { useMessagingWorkspaceOptional } from "./MessagingWorkspaceContext";
import { roomDisplayName } from "./types";

export interface RoomCommandItem {
  id: string;
  label: string;
  sublabel: string;
  run: () => void;
}

const LIMIT = 8;
const matches = (query: string, value: string) => !query || value.toLowerCase().includes(query.toLowerCase());

const KIND_LABEL = { channel: "Channel", dm: "Direct message", "group-dm": "Group message" } as const;

export function useRoomCommandResults(query: string, enabled: boolean): RoomCommandItem[] {
  const workspace = useMessagingWorkspaceOptional();
  const directory = workspace?.directoryProps;
  const openMessage = workspace?.openMessage;
  return useMemo(() => {
    if (!enabled || !directory || !openMessage) return [];
    const rooms = directory.rooms
      .map((room) => ({ room, label: roomDisplayName(room, room.participants) }))
      .filter((entry) => matches(query, entry.label))
      .slice(0, LIMIT)
      .map(({ room, label }) => ({
        id: `room:${room.roomId}`,
        label,
        sublabel: KIND_LABEL[room.kind],
        run: () => {
          openMessage(room.roomId, null);
          activateMessagesPanel();
        },
      }));
    const people = directory.participants
      .filter((participant) => matches(query, participant.displayName))
      .slice(0, LIMIT)
      .map((participant) => ({
        id: `participant:${participant.kind}:${participant.id}`,
        label: `Message ${participant.displayName}`,
        sublabel: participant.kind === "agent" ? "Agent" : "Person",
        run: () => {
          void directory.onSelectParticipant(participant);
          activateMessagesPanel();
        },
      }));
    return [...rooms, ...people];
  }, [enabled, directory, openMessage, query]);
}
```

- [ ] **Step 5: Add the Conversations group to `CommandPalette.tsx`**

Import `import { useRoomCommandResults } from "@/components/messaging/roomCommands";`.

Change the `FlatItem` type to:

```tsx
type FlatItem = {
  kind: "conversation" | "nav" | "agent" | "issue" | "project";
  id: string;
  label: string;
  path: string;
  run?: () => void;
};
```

In `CommandPaletteContent`, add `const conversations = useRoomCommandResults(query, open);` and put them first in `flatItems`:

```tsx
    conversations.forEach((c) => items.push({ kind: "conversation", id: c.id, label: c.label, path: "", run: c.run }));
```
(before the `navResults.forEach(...)` line), and add `conversations` to that `useMemo`'s dependency array.

Change `select`:

```tsx
  const select = (item: FlatItem) => {
    onOpenChange(false);
    if (item.run) { item.run(); return; }
    // Palette targets are all in-shell `/chat?...` URLs — commit them through
    // the History API (same mechanism as handleShellLinkClick) so a jump
    // costs no RSC round-trip on the force-dynamic /chat route.
    window.history.pushState(null, "", item.path);
  };
```

Render the group above `{navResults.length > 0 && (`:

```tsx
              {conversations.length > 0 && (
                <Group label="Conversations">
                  {conversations.map((c) => {
                    const idx = flatItems.findIndex((it) => it.kind === "conversation" && it.id === c.id);
                    return (
                      <Row
                        key={c.id}
                        label={c.label}
                        sublabel={c.sublabel}
                        selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)}
                        onClick={() => select(flatItems[idx])}
                      />
                    );
                  })}
                </Group>
              )}
```

- [ ] **Step 6: Update the surfaces doc**

In `references/surfaces.md`, replace the **Workspace search** row's description with:

> Click **Search agents, issues, and projects**, or press **⌘/Ctrl K**, to open the workspace command palette. It opens on **Conversations** — every channel and DM, plus "Message <name>" for anyone in the workspace — then Navigate, Agents, Issues and Projects.

- [ ] **Step 7: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging src/components/teams/command-palette && pnpm lint src/components/messaging src/components/teams/command-palette && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging apps/frontend/src/components/teams/command-palette apps/infra/openclaw/skills/lightbulb/references/surfaces.md
git commit -m "feat(messaging): conversations in the command palette"
```

---

### Task 9: `⌥` room navigation, `⌥⇧` unread navigation, and the `⌘/` shortcut sheet

*Model: sonnet — a global key handler that must not fight the composer or an open dialog.*

**Files:**
- Create: `apps/frontend/src/components/messaging/ShortcutSheet.tsx`
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx`
- Test: `apps/frontend/src/components/messaging/__tests__/ShortcutSheet.test.tsx` (create)
- Test: `apps/frontend/src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx` (append)

**Interfaces:**
- Produces: `export function ShortcutSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void })` — a `Dialog` titled `Keyboard shortcuts`; and a module-local `useMessagingShortcuts(rooms, activeRoomId, selectRoom, onShowShortcuts)` inside `MessagingWorkspaceContext.tsx`.

**Keyboard map (spec):** `⌘K` quick switcher (Task 8, owned by the palette) · `⌥↑ / ⌥↓` previous/next room · `⌥⇧↑ / ⌥⇧↓` previous/next unread · `Esc` close typeahead, then thread/overlay (typeahead ordering shipped in Task 2) · `⌘/` shortcut sheet. Room order is `rooms.rooms` order, wrapping at both ends. Both navigations are no-ops when there is no room to move to.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/frontend/src/components/messaging/__tests__/ShortcutSheet.test.tsx
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ShortcutSheet } from "../ShortcutSheet";

afterEach(cleanup);

it("lists the workspace keyboard map", () => {
  render(<ShortcutSheet open onOpenChange={vi.fn()} />);
  expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  for (const chord of ["⌘K", "⌥↑", "⌥↓", "⌥⇧↑", "⌥⇧↓", "Esc", "⌘/", "⌘B", "⌘I", "⌘⇧X", "⌘⇧C", "Enter", "Shift+Enter", "↑"]) {
    expect(screen.getByText(chord)).toBeInTheDocument();
  }
});

it("renders nothing while closed", () => {
  render(<ShortcutSheet open={false} onOpenChange={vi.fn()} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
```

```tsx
// append to apps/frontend/src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx
// Using this file's existing useRooms mock with three rooms (the second unread):
it("moves to the next room on Alt+ArrowDown and the next unread on Alt+Shift+ArrowDown", () => {
  // render the provider, then:
  // fireEvent.keyDown(window, { key: "ArrowDown", altKey: true })  -> selectRoom called with rooms[1].roomId
  // fireEvent.keyDown(window, { key: "ArrowDown", altKey: true, shiftKey: true }) -> selectRoom called with the unread room id
});

it("opens the shortcut sheet on Cmd+/", () => {
  // fireEvent.keyDown(window, { key: "/", metaKey: true })
  // expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
});
```

> Replace those two comment bodies with real code shaped to the file's existing `useRooms` mock — read it first and extend its fixture to three rooms, the second with `unreadCount: 2`, before writing the assertions. The behaviour under test is fixed by the comments; do not change it.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/ShortcutSheet.test.tsx src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx`
Expected: FAIL — `Cannot find module '../ShortcutSheet'`.

- [ ] **Step 3: Write `ShortcutSheet.tsx`**

```tsx
// apps/frontend/src/components/messaging/ShortcutSheet.tsx
"use client";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import "./messaging.css";

const GROUPS: { name: string; rows: [string, string][] }[] = [
  {
    name: "Move around",
    rows: [
      ["⌘K", "Jump to a conversation, person or agent"],
      ["⌥↑", "Previous conversation"],
      ["⌥↓", "Next conversation"],
      ["⌥⇧↑", "Previous unread conversation"],
      ["⌥⇧↓", "Next unread conversation"],
      ["Esc", "Close the typeahead, then the thread or overlay"],
      ["⌘/", "Show this sheet"],
    ],
  },
  {
    name: "Write",
    rows: [
      ["Enter", "Send"],
      ["Shift+Enter", "New line"],
      ["↑", "Edit your last message (empty composer)"],
      ["@", "Mention a person or agent"],
      [":", "Insert an emoji"],
      ["⌘B", "Bold"],
      ["⌘I", "Italic"],
      ["⌘⇧X", "Strikethrough"],
      ["⌘⇧C", "Code block"],
    ],
  },
];

export function ShortcutSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-lg max-h-[calc(100dvh-2rem)] overflow-y-auto">
      <DialogHeader><DialogTitle>Keyboard shortcuts</DialogTitle>
        <DialogDescription>On Windows and Linux, ⌘ is Ctrl and ⌥ is Alt.</DialogDescription></DialogHeader>
      {GROUPS.map((group) => <section key={group.name} aria-label={group.name}>
        <h4 className="font-semibold">{group.name}</h4>
        <dl className="room-shortcuts">{group.rows.map(([chord, description]) => <div key={chord}>
          <dt><kbd className="room-kbd">{chord}</kbd></dt><dd className="text-sm text-muted-foreground">{description}</dd>
        </div>)}</dl>
      </section>)}
    </DialogContent>
  </Dialog>;
}
```

CSS, appended to `messaging.css`:

```css
.room-shortcuts { display: grid; gap: 4px; margin: 8px 0 16px; }
.room-shortcuts > div { display: flex; align-items: baseline; gap: 12px; }
.room-shortcuts dt { flex: none; min-width: 88px; }
.room-kbd { display: inline-block; padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px; font-family: var(--font-mono); font-size: .75rem; color: var(--foreground); }
```

- [ ] **Step 4: Mount the shortcuts in `MessagingWorkspaceContext.tsx`**

Import `import { ShortcutSheet } from "./ShortcutSheet";`, add `const [shortcutsOpen, setShortcutsOpen] = useState(false);`, and add this effect:

```tsx
  const roomOrder = rooms.rooms;
  const activeRoomId = rooms.activeRoomId;
  const selectRoom = rooms.selectRoom;
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key === "/") {
        event.preventDefault();
        setShortcutsOpen((open) => !open);
        return;
      }
      if (!event.altKey || event.metaKey || event.ctrlKey) return;
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      const from = roomOrder.findIndex((room) => room.roomId === activeRoomId);
      const ordered = Array.from({ length: roomOrder.length }, (_, offset) =>
        roomOrder[(((from < 0 ? 0 : from) + step * (offset + 1)) % roomOrder.length + roomOrder.length) % roomOrder.length]);
      const next = event.shiftKey ? ordered.find((room) => room.unreadCount > 0) : ordered[0];
      if (!next || next.roomId === activeRoomId) return;
      event.preventDefault();
      selectRoom(next.roomId);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [roomOrder, activeRoomId, selectRoom]);
```

Render the sheet beside `<MessageNotifications …>`:

```tsx
  <ShortcutSheet open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
```

- [ ] **Step 5: Run, lint, scan, build, commit**

Run: `cd apps/frontend && pnpm test && pnpm lint && node scripts/brand-tokens-scan.mjs src && pnpm run build`
Expected: all green; `0`.

```bash
git add apps/frontend/src/components/messaging/ShortcutSheet.tsx apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__
git commit -m "feat(messaging): room navigation shortcuts and the shortcut sheet"
```

---

### Task 10 (controller, not an implementer): craft review, screenshots, ship Phase 2

*Model: opus — design judgement, review triage, and a prod promotion.*

**Files:** none directly; this task reviews, screenshots, ships, and fixes material findings in one batch.

- [ ] **Step 1: Impeccable detect.** From the repo root run
  `"/Users/prasiddhaparthsarthy/.claude/plugins/cache/impeccable/impeccable/4.2.2/skills/impeccable/scripts/impeccable" detect --json --target apps/frontend/src/components/messaging`
  (confirm the installed version path with `ls /Users/prasiddhaparthsarthy/.claude/plugins/cache/impeccable/impeccable/` first). Record findings in the PR description; fix the mechanical ones in this PR.
- [ ] **Step 2: Screenshot round** at **1440×900** and **390×844** on `https://dev.flickmeon.ai/chat`, covering: the `@` typeahead mid-query, the `:` typeahead, the formatting toolbar with a selection, the respond hint in a channel with three agents in three listen modes, the drag-over state, the Drafts sidebar section, `⌘K` showing Conversations first, and the `⌘/` sheet. Check the 44px touch floor and the wrapped toolbar at 390px.
- [ ] **Step 3: Finish review.** Spawn `impeccable:impeccable-finish-reviewer` with the spec's "Composer", "Keyboard map" and "States and accessibility" sections as the contract. Fix material findings in one batch; re-run `pnpm test && pnpm lint && node scripts/brand-tokens-scan.mjs src` after.
- [ ] **Step 4: Close #2477** as superseded, citing the four mismatches from the "Inputs considered" section of this plan. Do not merge it.
- [ ] **Step 5: PRs.** Open PR A (`feat(messaging): phase 2 (a) — composer typeaheads, formatting, drag-and-drop`, Tasks 1–6) and PR B (`feat(messaging): phase 2 (b) — drafts, respond hint, keyboard map`, Tasks 7–9) from `feat/slack-parity-phase-2`. Body: the acceptance line verbatim from the spec ("typing `@fr` + Enter mentions Friday and adds the structured mention; leaving and returning restores the draft; `⌘K` reaches any room in ≤3 keystrokes"), the detect output, the screenshots, and the Assumptions block from this plan's header. Wait for `claude-review`; check all four Codex surfaces before concluding it was silent (`gh pr view --json reviews`, `gh api …/pulls/<n>/comments`, `gh api …/issues/<n>/comments`, `gh api …/issues/<n>/reactions` — a bare 👍 is a complete approval).
- [ ] **Step 6: Merge and verify dev.** `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`. On `https://dev.flickmeon.ai/chat`: type `@fr` + Enter in a channel with Friday and confirm the message renders a mention chip **and** Friday actually runs; half-type in one room, switch, return, confirm the text is back; `⌘K` + one character + Enter opens a room.
- [ ] **Step 7: Prod.** Dispatch only when `gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 --json status --jq '[.[]|select(.status!="completed")]|length'` reads `0`; then `gh workflow run deploy.yml --ref main`; watch with `gh run watch`; founder check on `https://flickmeon.ai/chat`.

---

## Self-review

**Spec coverage (Phase 2 row: "`@` and `:` typeahead; toolbar + shortcuts; drag-and-drop images; per-room/thread drafts + Drafts row; who-will-respond hint; `⌘K` switcher; `⌥` room navigation; shortcut sheet"):**

| Spec item | Task |
|---|---|
| `@` typeahead, arrow keys + Enter/Tab, Escape, replaces the checkbox picker, `@` button opens the same list, agents first in a single-agent DM | 2 |
| Typed mentions resolve on send; unmatched tokens stay plain and promise nothing | 1 (grammar) + 2 (send path) + 7 (hint) |
| Human token is `@Display Name` (Open decision 1) | 1, 2 |
| `:` emoji typeahead after two characters; picker button inserts at the caret | 3 |
| Toolbar (bold, italic, strikethrough, ordered/unordered list, inline code, code block, link) + `⌘B / ⌘I / ⌘⇧X / ⌘⇧C` | 4 |
| `Enter` send · `Shift+Enter` newline | shipped, preserved and re-asserted by Task 2's last test |
| `↑` edits your last message | 5 (**not shipped despite the spec's claim**) |
| `Esc` closes the typeahead, then thread/overlay | 2 (`stopImmediatePropagation`) |
| Drag-and-drop onto the composer and the message list | 6 |
| Per-room and per-thread drafts, restored on return; Drafts sidebar row | 7 |
| Who-will-respond hint, all five rules, compact form | 7 |
| `⌘K` quick switcher over rooms + people + agents (Open decision 3) | 8 |
| `⌥↑/↓`, `⌥⇧↑/↓`, `⌘/` sheet | 9 |
| Agent-facing surfaces doc | 7 (Chat row) + 8 (Workspace search row), both with their `MAINTENANCE.md` anchors |
| `brand-tokens-scan` = 0, detect clean, screenshots at 1440/390, finish review, merge, dev check, prod dispatch | 10 |

Out of Phase 2 by the spec's own phasing and deliberately absent: backward history paging (Phase 3), stars/mute/sections/pins/topic (Phase 3), files and presence (Phase 4).

**Placeholder scan:** three steps hand off to a file the implementer must read first rather than inventing behaviour — Task 3's emoji-name confirmation, Task 7 Step 1's `MessagingWorkspaceContext` case, and Task 9 Step 1's two cases. Each states the exact behaviour under test and forbids changing it; the surrounding code is complete. Everything else carries full code.

**Type consistency:** `CompletionTrigger` (Task 1 → 2, 3); `applyCompletion` return `{ text, caret }` (Task 1 → 2, 3); `wrapSelection` return `{ text, selectionStart, selectionEnd }` (Task 1 → 4); `resolveTypedMentions(text, participants): MemberRef[]` (Task 1 → 2, and its output feeds `respondHint`'s `mentions` in Task 7); `RoomComposerProps` additions `onEditLast` (5), `dropTargetRef` (6), `roomKind`/`runningAgentIds` (7); `RoomMessageItemProps.startEditing` → `RoomMessageContent.startEditing` (5); `RoomDirectoryProps.draftRoomIds` (7); `useMessagingWorkspaceOptional` (8 only); `RoomCommandItem.run` → `FlatItem.run` (8). The composer's `mentions` const is the single effective list used by validation, the request, and the hint — no second derivation anywhere.
