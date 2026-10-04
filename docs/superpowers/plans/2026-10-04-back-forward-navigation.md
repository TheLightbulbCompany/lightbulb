# Back/Forward Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slack-style Back/Forward on the desktop web shell (and the Tauri app, which embeds it): rail buttons plus ⌘[ / ⌘], stepping through rooms, DMs, sessions and panels.

**Architecture:** Browser history is the only history. Panels already push `?panel=…` URLs through `lib/shellNav.ts`, and `WorkspaceShell`'s reconciler replays any URL change (including popstate). Room/DM/session picks are the gap: they change React state only. A desktop-only `ChatHistorySync` component writes `?panel=chat&room=…` / `?panel=chat&session=…` when the person (not the app) changes the chat view; the reconciler then executes it like any other URL. The buttons are `history.back()` / `history.forward()`, disabled from the Navigation API where it exists.

**Tech Stack:** Next.js 16 App Router (client), React 19, dockview, vitest + @testing-library/react (jsdom).

**Spec:** This conversation's agreed design. No parallel history stack; URL stays a one-way command channel (`components/workspace/urlChannel.ts` header); dock tab clicks and thread opens are NOT recorded. Revised after review: only explicit picks are recorded (automatic room picks — cold-load default, fresh-workspace #general swap, re-pick after leaving/deleting a room — are not); the reconciler's replay of our own push is made a no-op in `ChatPanel`.

## Global Constraints

- Desktop shell only. `MessagingWorkspaceProvider` is shared with the mobile web shell (`MobileChatShell`), so nothing in that provider may write the URL. The provider change in Task 2 only counts explicit picks. All URL writes live under `components/workspace/`.
- URL writes go through `pushSearchParams` / `replaceSearchParams` in `apps/frontend/src/lib/shellNav.ts` — never `router.push`.
- The reconciler (`WorkspaceShell.tsx` `runReconciler`) stays the ONLY code that turns a URL into a view change. New code only writes URLs (plus Task 2's "bare URL → show Messages" popstate fallback).
- Colours: reuse the rail's existing declarations (`workspace.css:348-396`) so the new buttons match; no literals (`src/__tests__/brandTokens.test.ts`).
- Do not run whole test suites locally (owner rule). Run only the targeted commands each step names: `cd apps/frontend && pnpm exec vitest run <file>` and, before commit, `pnpm exec vitest related --run <changed files>`. CI is the full gate.
- Fresh worktree deps: symlink `node_modules`, `apps/frontend/node_modules`, `packages/shared/node_modules` from `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync`; remove the symlinks before committing.

## Review Focus

1. Cold load on bare `/chat` in a FRESH workspace still auto-opens `#general`: `activeRoomId` starts `null` (`useRooms.ts:295`), then auto-picks; neither the auto-pick nor the #general swap is explicit, so nothing is written and `markExplicitChoice` is never triggered by us. Tests: Task 1 (`explicit=false` writes nothing; null views ignored).
2. A permalink / notification that scrolls to a message keeps its scroll target: the replay of our room-only push must not call `openMessage(room, null)`. Test: Task 2 ChatPanel case.
3. Back never creates an entry: a replayed view equals the URL. Test: Task 1.
4. Back onto a bare `/chat` entry (cold load, or after `clearPanelFromUrl` replaced a closed panel's URL with nothing) shows Messages instead of doing nothing. Test: Task 2.
5. ⌘[ while ⌘K, a dialog or a menu is open does nothing; works on layouts where `[` needs Alt (uses `event.code`). Test: Task 3.

Known and accepted: an echo of a pushed room still focuses the Messages panel (`addOrFocus` → `setActive`), so ⌥↓ pressed while Tasks has focus moves focus to Messages. History entries for a room the person later left or deleted fail to open on Back, like a dead link.

---

### Task 1: Pure chat-history planner

**Files:**
- Create: `apps/frontend/src/components/workspace/chatHistory.ts`
- Test: `apps/frontend/src/components/workspace/__tests__/chatHistory.test.ts`

**Interfaces:**
- Consumes: `parseCommand(search: string): WorkspaceCommand | null` from `./urlChannel`; `pushSearchParams`, `replaceSearchParams` from `@/lib/shellNav`.
- Produces:
  - `interface ChatView { roomId: string | null; sessionId: string | null }`
  - `chatViewSearch(view: ChatView): string | null`
  - `planChatHistoryWrite(prev: ChatView | null, next: ChatView, currentSearch: string): { replace?: string; push?: string } | null`
  - `recordChatView(next: ChatView, explicit: boolean): void`
  - `recordMessagesNavigation(): void`
  - `resetChatView(): void`

- [ ] **Step 1: Write the failing test**

```ts
// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import {
  chatViewSearch,
  planChatHistoryWrite,
  recordChatView,
  recordMessagesNavigation,
  resetChatView,
} from "../chatHistory";

const room = (roomId: string) => ({ roomId, sessionId: null });

afterEach(() => {
  resetChatView();
  window.history.replaceState(null, "", "/chat");
});

describe("chatViewSearch", () => {
  it("prefers the session over the room", () => {
    expect(chatViewSearch({ roomId: "r1", sessionId: "s1" })).toBe("panel=chat&session=s1");
    expect(chatViewSearch(room("r1"))).toBe("panel=chat&room=r1");
    expect(chatViewSearch({ roomId: null, sessionId: null })).toBeNull();
  });
});

describe("planChatHistoryWrite", () => {
  it("does nothing when the URL already shows the view (a Back replay)", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=chat&room=r2")).toBeNull();
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=chat&room=r2&message=m9")).toBeNull();
  });

  it("pushes a room pick made from a panel URL", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=teams-issues")).toEqual({ push: "panel=chat&room=r2" });
  });

  it("replaces a bare /chat entry with the previous room before pushing", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "")).toEqual({
      replace: "panel=chat&room=r1",
      push: "panel=chat&room=r2",
    });
  });

  it("replaces, not pushes, an agent deep link once it resolves to a room", () => {
    expect(planChatHistoryWrite(room("r1"), room("dm"), "?agent=a1")).toEqual({ replace: "panel=chat&room=dm" });
    expect(planChatHistoryWrite(room("r1"), room("dm"), "?panel=chat&agent=a1")).toEqual({ replace: "panel=chat&room=dm" });
  });

  it("does not treat a session URL as showing its room", () => {
    expect(planChatHistoryWrite({ roomId: "r1", sessionId: "s1" }, room("r1"), "?panel=chat&session=s1"))
      .toEqual({ push: "panel=chat&room=r1" });
  });

  it("never replaces with the same view it pushes", () => {
    expect(planChatHistoryWrite(room("r1"), room("r1"), "")).toEqual({ push: "panel=chat&room=r1" });
  });
});

describe("recordChatView", () => {
  it("writes nothing for automatic picks or empty views", () => {
    const before = window.history.length;
    recordChatView({ roomId: null, sessionId: null }, false);
    recordChatView(room("r1"), false);
    recordChatView(room("general"), false);
    expect(window.location.search).toBe("");
    expect(window.history.length).toBe(before);
  });

  it("writes explicit picks, replacing the bare entry with the automatic room", () => {
    recordChatView(room("r1"), false);
    const before = window.history.length;
    recordChatView(room("r2"), true);
    expect(window.location.search).toBe("?panel=chat&room=r2");
    expect(window.history.length).toBe(before + 1);
  });

  it("re-records the open view for the rail's Messages button", () => {
    recordChatView(room("r1"), false);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    recordMessagesNavigation();
    expect(window.location.search).toBe("?panel=chat&room=r1");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/components/workspace/__tests__/chatHistory.test.ts`
Expected: FAIL — `Cannot find module '../chatHistory'`.

- [ ] **Step 3: Write minimal implementation**

```ts
// Room, DM and session picks join the shell's URL command channel so browser
// history — and the rail's Back/Forward — steps through them. This module only
// WRITES the URL; WorkspaceShell's reconciler stays the one executor and replays
// it (spec §6: the URL is a one-way command channel). Desktop shell only: the
// messaging provider is shared with the mobile web shell and must not write URLs.
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import { parseCommand } from "./urlChannel";

export interface ChatView {
  roomId: string | null;
  sessionId: string | null;
}

export function chatViewSearch(view: ChatView): string | null {
  if (view.sessionId) return new URLSearchParams({ panel: "chat", session: view.sessionId }).toString();
  if (view.roomId) return new URLSearchParams({ panel: "chat", room: view.roomId }).toString();
  return null;
}

function params(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
}

function urlShows(search: string, view: ChatView): boolean {
  const p = params(search);
  if (p.get("panel") !== "chat") return false;
  if (view.sessionId) return p.get("session") === view.sessionId;
  return !p.get("session") && p.get("room") === view.roomId;
}

const sameView = (a: ChatView, b: ChatView) => a.roomId === b.roomId && a.sessionId === b.sessionId;

export function planChatHistoryWrite(
  prev: ChatView | null,
  next: ChatView,
  currentSearch: string,
): { replace?: string; push?: string } | null {
  const target = chatViewSearch(next);
  if (!target || urlShows(currentSearch, next)) return null;
  const p = params(currentSearch);
  // An agent deep link (?agent= or ?panel=chat&agent=) just resolved to this
  // room: same navigation, so it is rewritten in place, not stacked.
  if ((p.get("panel") === "chat" || (!p.get("panel") && p.get("agent"))) && !p.get("room") && !p.get("session")) {
    return { replace: target };
  }
  // A bare /chat entry carries no command, so Back onto it would show nothing
  // new; it becomes the view it was actually showing.
  const replace = prev && !sameView(prev, next) && parseCommand(currentSearch) === null
    ? chatViewSearch(prev)
    : null;
  return replace ? { replace, push: target } : { push: target };
}

// Module state, like workspaceController's dock singleton: one desktop shell per page.
let current: ChatView | null = null;

function apply(write: { replace?: string; push?: string }): void {
  if (write.replace) replaceSearchParams(new URLSearchParams(write.replace));
  if (write.push) pushSearchParams(new URLSearchParams(write.push));
}

/** `explicit`: the person picked this view. Automatic picks are only remembered —
 *  writing them would replay chooseRoom, mark an explicit choice, and stop a fresh
 *  workspace auto-opening #general. */
export function recordChatView(next: ChatView, explicit: boolean): void {
  if (!chatViewSearch(next)) return;
  const prev = current;
  current = next;
  if (!explicit) return;
  const write = planChatHistoryWrite(prev, next, window.location.search);
  if (write) apply(write);
}

/** The rail's Messages button is a navigation to the chat already open. */
export function recordMessagesNavigation(): void {
  if (!current) return;
  const write = planChatHistoryWrite(current, current, window.location.search);
  if (write) apply(write);
}

export function resetChatView(): void {
  current = null;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm exec vitest run src/components/workspace/__tests__/chatHistory.test.ts`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/workspace/chatHistory.ts apps/frontend/src/components/workspace/__tests__/chatHistory.test.ts
git commit -m "feat(workspace): plan chat-view history writes"
```

---

### Task 2: Record explicit room, DM and session picks in the desktop shell

**Files:**
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (an `explicitPicks` counter: declared next to `explicitChoiceScope` ~line 250, incremented in `markExplicitChoice`, exposed on the context type ~line 30 and the provider value ~line 850)
- Create: `apps/frontend/src/components/workspace/ChatHistorySync.tsx`
- Modify: `apps/frontend/src/components/workspace/panels/ChatPanel.tsx` (room-intent effect ~line 64-77)
- Modify: `apps/frontend/src/components/workspace/WorkspaceShell.tsx` (render `<ChatHistorySync />` right after `<DesktopPermissionsGate />`, ~line 450)
- Modify: `apps/frontend/src/components/workspace/WorkspaceHeader.tsx` (Messages rail button `onClick`, ~line 82)
- Test: `apps/frontend/src/components/workspace/__tests__/ChatHistorySync.test.tsx` (new)
- Test: `apps/frontend/src/components/workspace/__tests__/ChatPanel.test.tsx` (one case + mock fields)
- Test: `apps/frontend/src/components/workspace/__tests__/WorkspaceHeader.test.tsx` (one case + mock)
- Test: `apps/frontend/src/components/workspace/__tests__/WorkspaceShell.test.tsx` (mock `ChatHistorySync`)

**Interfaces:**
- Consumes: Task 1; `parseCommand` from `./urlChannel`; `activateMessagesPanel` from `./workspaceController`.
- Produces:
  - Context field `explicitPicks: React.MutableRefObject<number>` — increments on every explicit choice (`chooseRoom`, DM/participant picks, explicit agent DM, link/notification opens; they all route through `markExplicitChoice`).
  - `ChatHistorySync(): null`.

How it fits together: a person's pick marks an explicit choice (counter +1) → the view changes → `ChatHistorySync` sees the counter moved and pushes the URL → the reconciler runs `openMessagePanel` → `ChatPanel` sees the URL's room is already open and does nothing (no `openMessage`, so no `closeSession`/`setTarget`/second `selectRoom`). Back changes the URL first → the reconciler → `ChatPanel` sees a different room → `openMessage` → `chooseRoom` (explicit) → view equals the URL → `planChatHistoryWrite` returns null. Session views are always explicit (nothing opens a session automatically; a deep link's URL already shows it). The new-session composer and the all-sessions list (`sessionFocus` with `sessionId: null`) are not recordable views.

- [ ] **Step 1: Write the failing tests**

`ChatHistorySync.test.tsx`:

```tsx
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";

type Focus = { sessionId: string | null; all?: boolean } | null;
const workspace = vi.hoisted(() => ({
  value: { activeRoomId: null as string | null, sessionFocus: null as Focus, explicitPicks: { current: 0 } },
}));
const activateMessagesPanel = vi.hoisted(() => vi.fn());
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspace: () => workspace.value,
}));
vi.mock("../workspaceController", () => ({ activateMessagesPanel }));

import { ChatHistorySync } from "../ChatHistorySync";

function view(activeRoomId: string | null, sessionFocus: Focus = null, picked = false) {
  if (picked) workspace.value.explicitPicks.current += 1;
  workspace.value = { ...workspace.value, activeRoomId, sessionFocus };
}

afterEach(() => {
  window.history.replaceState(null, "", "/chat");
  workspace.value = { activeRoomId: null, sessionFocus: null, explicitPicks: { current: 0 } };
  vi.clearAllMocks();
});

describe("ChatHistorySync", () => {
  it("leaves the cold load's automatic picks alone", () => {
    const shell = render(<ChatHistorySync />);
    view("vendor");
    shell.rerender(<ChatHistorySync />);
    view("general");
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("");
    shell.unmount();
  });

  it("pushes a person's pick", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    view("r2", null, true);
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&room=r2");
    shell.unmount();
  });

  it("records an opened session but not the new-session composer", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    view("r1", { sessionId: "s1" });
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&session=s1");
    view("r1", { sessionId: null });
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&session=s1");
    shell.unmount();
  });

  it("shows Messages when Back lands on a bare URL", () => {
    const shell = render(<ChatHistorySync />);
    act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(activateMessagesPanel).toHaveBeenCalledTimes(1);
    window.history.replaceState(null, "", "/chat?panel=teams-issues");
    act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(activateMessagesPanel).toHaveBeenCalledTimes(1);
    shell.unmount();
  });

  it("forgets the view on unmount, so another workspace starts clean", () => {
    view("r1");
    render(<ChatHistorySync />).unmount();
    view("other-ws-room", null, true);
    const next = render(<ChatHistorySync />);
    expect(window.location.search).toBe("");
    next.unmount();
  });
});
```

`ChatPanel.test.tsx`: extend the existing context mock to carry the new fields, e.g. change it to

```tsx
let activeRoomId: string | null = null;
let sessionFocus: { sessionId: string | null } | null = null;
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspace: () => ({ selectAgentDm, openSession, openMessage, freshLanding, activeRoomId, sessionFocus }),
}));
```

reset both to `null` in the file's existing `beforeEach`, and add:

```tsx
  it("does not re-open the room that is already open (the replay of its own URL)", () => {
    activeRoomId = "r1";
    const props = makeProps();
    props.params = { roomId: "r1" };
    render(<ChatPanel {...props} />);
    expect(openMessage).not.toHaveBeenCalled();
    act(() => props.api.updateParameters({ roomId: "r1", messageId: "m1" } as never));
    expect(openMessage).toHaveBeenLastCalledWith("r1", "m1");
    act(() => props.api.updateParameters({ roomId: "r2" } as never));
    expect(openMessage).toHaveBeenLastCalledWith("r2", null);
  });
```

`WorkspaceHeader.test.tsx`: add with the other mocks (before the `WorkspaceHeader` import)

```tsx
const recordMessagesNavigation = vi.fn();
vi.mock("../chatHistory", () => ({ recordMessagesNavigation: () => recordMessagesNavigation() }));
```

and inside `describe("WorkspaceHeader", …)`:

```tsx
  it("records the Messages button as a navigation", () => {
    render(<WorkspaceHeader />);
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(recordMessagesNavigation).toHaveBeenCalled();
  });
```

`WorkspaceShell.test.tsx`: its `MessagingWorkspaceContext` mock exports only the provider, so stub the new component next to the other `vi.mock` calls:

```tsx
vi.mock("../ChatHistorySync", () => ({ ChatHistorySync: () => null }));
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/frontend && pnpm exec vitest run src/components/workspace/__tests__/ChatHistorySync.test.tsx src/components/workspace/__tests__/ChatPanel.test.tsx src/components/workspace/__tests__/WorkspaceHeader.test.tsx`
Expected: FAIL — missing `../ChatHistorySync`; the new ChatPanel case calls `openMessage`; the header case misses `recordMessagesNavigation`.

- [ ] **Step 3: Implement**

`MessagingWorkspaceContext.tsx`:

```tsx
  // Counts every explicit choice. The desktop shell's history (ChatHistorySync)
  // records a view change only when this moved, so automatic picks stay out of it.
  const explicitPicks = useRef(0);
  const markExplicitChoice = useCallback(() => {
    explicitChoiceScope.current = scope;
    explicitPicks.current += 1;
  }, [scope]);
```

(replace the existing one-line `markExplicitChoice`). Add `explicitPicks: React.MutableRefObject<number>;` to the context interface and `explicitPicks,` to the provider value. If the file already has a test that builds the context value shape exactly (`MessagingWorkspaceContext.test.tsx`), add the field there.

`ChatHistorySync.tsx`:

```tsx
"use client";

import { useEffect, useRef } from "react";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { recordChatView, resetChatView } from "./chatHistory";
import { parseCommand } from "./urlChannel";
import { activateMessagesPanel } from "./workspaceController";

/** Puts the person's room, DM and session picks into browser history (desktop shell only). */
export function ChatHistorySync(): null {
  const { activeRoomId, sessionFocus, explicitPicks } = useMessagingWorkspace();
  const seenPicks = useRef(explicitPicks.current);
  const sessionId = sessionFocus?.sessionId ?? null;
  // The new-session composer and the all-sessions list are not places to go back to.
  const composing = !!sessionFocus && !sessionFocus.sessionId;
  useEffect(() => {
    if (composing) return;
    const explicit = explicitPicks.current !== seenPicks.current || !!sessionId;
    seenPicks.current = explicitPicks.current;
    recordChatView({ roomId: activeRoomId, sessionId }, explicit);
  }, [activeRoomId, sessionId, composing, explicitPicks]);
  useEffect(() => {
    // A bare /chat entry carries no command for the reconciler; Back onto one
    // shows Messages instead of leaving the last panel on screen.
    const onPop = () => { if (parseCommand(window.location.search) === null) activateMessagesPanel(); };
    window.addEventListener("popstate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      resetChatView();
    };
  }, []);
  return null;
}
```

`ChatPanel.tsx`: read `activeRoomId` and `sessionFocus` from `useMessagingWorkspace()` alongside the existing fields, and change the room-intent effect's tail (after `++navigationRequest.current;`):

```tsx
    if (params.sessionId) openSession(params.sessionId);
    // The replay of a room the person just picked (ChatHistorySync wrote its URL)
    // must not re-open it: that would clear a scroll target and close a session view.
    else if (params.roomId && (params.messageId || params.roomId !== activeRoomId || sessionFocus)) {
      openMessage(params.roomId, params.messageId ?? null);
    }
  }, [params.roomId, params.messageId, params.sessionId, params.sessionIntent, openMessage, openSession, activeRoomId, sessionFocus]);
```

(`lastOpenedRoom` still guards re-runs, so the extra deps do not re-open anything.)

`WorkspaceShell.tsx`: `import { ChatHistorySync } from "./ChatHistorySync";` and render `<ChatHistorySync />` immediately after `<DesktopPermissionsGate />`.

`WorkspaceHeader.tsx`: `import { recordMessagesNavigation } from "./chatHistory";` and:

```tsx
            if (panel === MESSAGES_PANEL_ID) { activateMessagesPanel(); recordMessagesNavigation(); }
            else openPanel(panel, {}, { pushUrl: true });
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm exec vitest run src/components/workspace/__tests__/ChatHistorySync.test.tsx src/components/workspace/__tests__/ChatPanel.test.tsx src/components/workspace/__tests__/WorkspaceHeader.test.tsx src/components/workspace/__tests__/WorkspaceShell.test.tsx`
Expected: PASS.
Then: `cd apps/frontend && pnpm exec vitest related --run src/components/messaging/MessagingWorkspaceContext.tsx src/components/workspace/panels/ChatPanel.tsx src/components/workspace/WorkspaceShell.tsx src/components/workspace/WorkspaceHeader.tsx src/components/workspace/ChatHistorySync.tsx`
Expected: PASS. Any other test that mocks `useMessagingWorkspace` for `ChatPanel` needs `activeRoomId`/`sessionFocus` in its mock; add them there, do not guard in the component.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx apps/frontend/src/components/workspace/ChatHistorySync.tsx apps/frontend/src/components/workspace/panels/ChatPanel.tsx apps/frontend/src/components/workspace/WorkspaceShell.tsx apps/frontend/src/components/workspace/WorkspaceHeader.tsx apps/frontend/src/components/workspace/__tests__
git commit -m "feat(workspace): record room, DM and session picks in browser history"
```

---

### Task 3: Back/Forward buttons and ⌘[ / ⌘]

**Files:**
- Create: `apps/frontend/src/components/workspace/HistoryButtons.tsx`
- Modify: `apps/frontend/src/components/workspace/WorkspaceHeader.tsx` (render `<HistoryButtons />` as the rail's first child, above the search button)
- Modify: `apps/frontend/src/components/workspace/workspace.css` (append `.workspace-rail__history` rules)
- Modify: `apps/frontend/src/components/messaging/ShortcutSheet.tsx` ("Move around" group, two rows)
- Test: `apps/frontend/src/components/workspace/__tests__/HistoryButtons.test.tsx`

**Interfaces:**
- Consumes: `hasBlockingShortcutDialog(root?: ParentNode): boolean` from `@/components/teams/shared/lib/keyboardShortcuts`.
- Produces: `HistoryButtons(): React.ReactElement`; `historyAvailability(): { back: boolean; forward: boolean }`.

Where `window.navigation` is missing (possibly the Tauri app's WKWebView — verify in acceptance), both buttons stay enabled; Back at the first entry is then a silent no-op, or leaves `/chat` if the previous entry was another page.

- [ ] **Step 1: Write the failing test**

```tsx
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { HistoryButtons, historyAvailability } from "../HistoryButtons";

type Win = Window & { navigation?: unknown };

function fakeNavigation(paths: string[], index: number) {
  (window as Win).navigation = {
    entries: () => paths.map((path) => ({ url: `${window.location.origin}${path}` })),
    currentEntry: { index },
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

beforeEach(() => window.history.replaceState(null, "", "/chat"));
afterEach(() => {
  delete (window as Win).navigation;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("historyAvailability", () => {
  it("stays enabled where the Navigation API is missing", () => {
    expect(historyAvailability()).toEqual({ back: true, forward: true });
  });

  it("only steps to entries inside the app", () => {
    fakeNavigation(["/sign-in", "/chat", "/chat?panel=teams-issues"], 1);
    expect(historyAvailability()).toEqual({ back: false, forward: true });
    fakeNavigation(["/chat", "/chat?panel=chat&room=r1"], 1);
    expect(historyAvailability()).toEqual({ back: true, forward: false });
  });
});

describe("HistoryButtons", () => {
  it("steps back and forward", () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    const forward = vi.spyOn(window.history, "forward").mockImplementation(() => {});
    render(<HistoryButtons />);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(back).toHaveBeenCalledTimes(1);
    expect(forward).toHaveBeenCalledTimes(1);
  });

  it("maps ⌘[ and ⌘] by key position, so layouts that need Alt for [ still work", () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    const forward = vi.spyOn(window.history, "forward").mockImplementation(() => {});
    render(<HistoryButtons />);
    fireEvent.keyDown(window, { code: "BracketLeft", key: "[", metaKey: true });
    fireEvent.keyDown(window, { code: "BracketRight", key: "]", ctrlKey: true });
    fireEvent.keyDown(window, { code: "BracketLeft", key: "[", metaKey: true, altKey: true });
    expect(back).toHaveBeenCalledTimes(2);
    expect(forward).toHaveBeenCalledTimes(1);
  });

  it("ignores ⌘[ while a dialog or menu is open", () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    render(<HistoryButtons />);
    for (const role of ["dialog", "menu"]) {
      const overlay = document.createElement("div");
      overlay.setAttribute("role", role);
      document.body.appendChild(overlay);
      fireEvent.keyDown(window, { code: "BracketLeft", key: "[", metaKey: true });
      overlay.remove();
    }
    expect(back).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/components/workspace/__tests__/HistoryButtons.test.tsx`
Expected: FAIL — `Cannot find module '../HistoryButtons'`.

- [ ] **Step 3: Implement**

`HistoryButtons.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { hasBlockingShortcutDialog } from "@/components/teams/shared/lib/keyboardShortcuts";

interface NavigationLike {
  entries?: () => { url: string | null }[];
  currentEntry?: { index: number } | null;
  addEventListener?: (type: "currententrychange", listener: () => void) => void;
  removeEventListener?: (type: "currententrychange", listener: () => void) => void;
}

const navigationApi = () => (window as Window & { navigation?: NavigationLike }).navigation;

/** Back/Forward stay inside the app: the entry beside this one must be this page. */
export function historyAvailability(): { back: boolean; forward: boolean } {
  const navigation = navigationApi();
  const index = navigation?.currentEntry?.index;
  if (!navigation?.entries || index === undefined) return { back: true, forward: true };
  const entries = navigation.entries();
  const inApp = (entry?: { url: string | null }) =>
    !!entry?.url && new URL(entry.url).pathname === window.location.pathname;
  return { back: inApp(entries[index - 1]), forward: inApp(entries[index + 1]) };
}

export function HistoryButtons(): React.ReactElement {
  const [available, setAvailable] = useState({ back: true, forward: true });
  useEffect(() => {
    const update = () => setAvailable(historyAvailability());
    update();
    const navigation = navigationApi();
    navigation?.addEventListener?.("currententrychange", update);
    return () => navigation?.removeEventListener?.("currententrychange", update);
  }, []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.shiftKey) return;
      if (!(event.metaKey || event.ctrlKey)) return;
      if (event.code !== "BracketLeft" && event.code !== "BracketRight") return;
      if (hasBlockingShortcutDialog() || document.querySelector("[role='menu']")) return;
      event.preventDefault();
      if (event.code === "BracketLeft") window.history.back();
      else window.history.forward();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className="workspace-rail__history">
      <button type="button" aria-label="Back" title="Back (⌘[)" disabled={!available.back} onClick={() => window.history.back()}>
        <ArrowLeft size={16} aria-hidden="true" />
      </button>
      <button type="button" aria-label="Forward" title="Forward (⌘])" disabled={!available.forward} onClick={() => window.history.forward()}>
        <ArrowRight size={16} aria-hidden="true" />
      </button>
    </div>
  );
}
```

`WorkspaceHeader.tsx`: `import { HistoryButtons } from "./HistoryButtons";` and render `<HistoryButtons />` as the first child of `<nav className="workspace-rail" …>`.

`workspace.css` (append; the rail's `> button` rules are direct-child selectors, so these copy their hover/disabled/focus declarations — `workspace.css:382-396` — for the nested buttons):

```css
/* Back/Forward: two compact arrows in one rail slot. */
.workspace-rail__history { display: flex; flex: none; gap: 2px; width: 44px; }
.workspace-rail__history > button {
  display: flex; flex: 1; align-items: center; justify-content: center;
  height: 28px; border-radius: 7px; color: var(--midnight-muted); cursor: pointer;
}
.workspace-rail__history > button:hover:not(:disabled) { color: var(--midnight); background: var(--cream-deeper); }
.workspace-rail__history > button:disabled { opacity: 0.5; cursor: not-allowed; }
.workspace-rail__history > button:focus-visible { outline: 2px solid var(--midnight); outline-offset: 1px; }
```

`ShortcutSheet.tsx`, "Move around" rows, insert after `["⌘K", …]`:

```ts
      ["⌘[", "Back"],
      ["⌘]", "Forward"],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm exec vitest run src/components/workspace/__tests__/HistoryButtons.test.tsx`
Expected: PASS (5 tests).
Then: `cd apps/frontend && pnpm exec vitest related --run src/components/workspace/HistoryButtons.tsx src/components/workspace/WorkspaceHeader.tsx src/components/messaging/ShortcutSheet.tsx`
Expected: PASS. If `ShortcutSheet.test.tsx` or a `WorkspaceHeader.test.tsx` case asserts an exact row/button list, add the new entries to its expectation.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/workspace/HistoryButtons.tsx apps/frontend/src/components/workspace/WorkspaceHeader.tsx apps/frontend/src/components/workspace/workspace.css apps/frontend/src/components/messaging/ShortcutSheet.tsx apps/frontend/src/components/workspace/__tests__/HistoryButtons.test.tsx
git commit -m "feat(workspace): Back/Forward rail buttons and ⌘[ / ⌘]"
```

---

### Task 4: PR and dev acceptance

**Files:** none (verification + PR). Delete this plan file in the PR (CLAUDE.md: planning docs are disposable; the PR description is the record).

- [ ] **Step 1: Type-check** — `cd apps/frontend && pnpm exec tsc --noEmit -p .` → exit 0 (an error that also reproduces on `origin/main` from stale symlinked deps is noted in the PR, not fixed).
- [ ] **Step 2: Remove dep symlinks, `git rm` the plan, push, open the PR** — `gh pr create --repo TheLightbulbCompany/lightbulb --title "feat(workspace): Slack-style Back/Forward" --body "<summary + test plan; end with the Claude Code attribution line>"`.
- [ ] **Step 3: Watch CI** — `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60`; check Codex on all four surfaces (CLAUDE.md "Code Review").
- [ ] **Step 4: Dev acceptance after merge (Chrome, dev.flickmeon.ai, Clerk ticket sign-in per CLAUDE.md)** — receipts on the PR:
  1. Room A → room B → Tasks → a DM → Back ×3 lands on Tasks, B, A in order; Forward returns; Back once more lands on the first entry and shows Messages.
  2. ⌘[ / ⌘] do the same; with ⌘K open they do nothing.
  3. Fresh dev workspace cold load lands on #general, Back is disabled (Chrome has the Navigation API).
  4. A notification / in-chat message link scrolls to its message and stays there.
  5. Opening a session, then "New session", then Back returns to the session.
  6. Hard reload on a `?panel=chat&room=…` URL opens that room.
  7. No visible Suspense flash or network RSC request on Back/Forward (DevTools Network).
  8. After the prod promotion: in the Tauri desktop app, Back/Forward and ⌘[ / ⌘] work, and note whether the buttons ever disable (tells us if WKWebView has `window.navigation`).
