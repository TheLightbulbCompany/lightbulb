# Dockable Workspace PR-1: Extractions + Hazard Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land every extraction and singleton-hazard fix the dockview workspace shell (PR-2) depends on, with zero behavior change to the classic shell.

**Architecture:** Pure-move extractions from `ChatLayout.tsx` into `src/components/chat/shell/` hooks that both the classic shell (now) and `WorkspaceShell` (PR-2) consume; plus five self-contained hazard fixes (storage-listener key filters, Cmd+S scoping, FileViewer style dedupe, ChatErrorCard router-Link removal, chat transcript revalidate-on-mount). Spec: `docs/superpowers/specs/2026-07-11-dockable-workspace-design.md` (§4 hazards, §9 PR-1).

**Tech Stack:** Next.js 16 App Router, React 19, SWR, vitest + @testing-library/react, TypeScript.

## Global Constraints

- Work in worktree `.claude/worktrees/dockable-workspace`, branch `feat/dockable-workspace`. All paths below are relative to `apps/frontend/`.
- **Zero behavior change** on the classic shell (one deliberate exception: Task 4 tightens Cmd+S scope — documented there).
- **NEVER run the full frontend vitest suite** (RAM-heavy). Single-file runs only: `pnpm vitest run <path> --silent`.
- Run `pnpm exec tsc --noEmit` after every task that adds/changes TypeScript (CI typechecks test files too).
- Do NOT touch `src/components/teams/inbox/InboxPage.tsx` or `src/components/teams/inbox/hooks/*` — concurrent human work in flight.
- No `sleep`/`tail` in bash. No push to origin without explicit user approval (final task stops for it).
- Line numbers were verified against origin/main at 326fbd18f; re-locate by the quoted code if drifted.

---

### Task 1: localStorage/sessionStorage test polyfill helper

Node 22 vitest has no working localStorage; 6+ test files repeat a Map-backed polyfill. Centralize it for the new tests in this plan (existing tests are NOT migrated — churn without benefit).

**Files:**
- Create: `src/test-utils/storagePolyfill.ts`
- Test: `src/test-utils/__tests__/storagePolyfill.test.ts`

**Interfaces:**
- Produces: `installStoragePolyfill(): () => void` — installs Map-backed `localStorage` + `sessionStorage` on `globalThis` and `window`, returns a restore function. Used by Tasks 2 and 7 tests.

- [ ] **Step 1: Write the failing test**

```ts
// src/test-utils/__tests__/storagePolyfill.test.ts
import { describe, it, expect } from "vitest";
import { installStoragePolyfill } from "../storagePolyfill";

describe("installStoragePolyfill", () => {
  it("provides working local/session storage and restores on cleanup", () => {
    const restore = installStoragePolyfill();
    window.localStorage.setItem("k", "v");
    expect(window.localStorage.getItem("k")).toBe("v");
    window.sessionStorage.setItem("s", "1");
    expect(window.sessionStorage.getItem("s")).toBe("1");
    window.localStorage.removeItem("k");
    expect(window.localStorage.getItem("k")).toBeNull();
    window.localStorage.setItem("a", "b");
    window.localStorage.clear();
    expect(window.localStorage.getItem("a")).toBeNull();
    restore();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/test-utils/__tests__/storagePolyfill.test.ts --silent`
Expected: FAIL — `Cannot find module '../storagePolyfill'`

- [ ] **Step 3: Write the implementation**

```ts
// src/test-utils/storagePolyfill.ts
// Node 22's built-in localStorage needs --localstorage-file; vitest runs
// without it, so storage-touching tests install this Map-backed polyfill.
// Mirrors the inline pattern in inboxStorage.test.ts / desktopPermissions.test.ts.
function makeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => (map.has(key) ? map.get(key)! : null),
    key: (index: number) => Array.from(map.keys())[index] ?? null,
    removeItem: (key: string) => {
      map.delete(key);
    },
    setItem: (key: string, value: string) => {
      map.set(key, String(value));
    },
  } as Storage;
}

export function installStoragePolyfill(): () => void {
  const localDesc = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const sessionDesc = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  const local = makeStorage();
  const session = makeStorage();
  for (const target of [globalThis, window] as object[]) {
    Object.defineProperty(target, "localStorage", { value: local, configurable: true });
    Object.defineProperty(target, "sessionStorage", { value: session, configurable: true });
  }
  return () => {
    for (const [name, desc] of [
      ["localStorage", localDesc],
      ["sessionStorage", sessionDesc],
    ] as const) {
      if (desc) Object.defineProperty(globalThis, name, desc);
      else delete (globalThis as Record<string, unknown>)[name];
    }
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run src/test-utils/__tests__/storagePolyfill.test.ts --silent`
Expected: PASS

- [ ] **Step 5: Typecheck and commit**

```bash
pnpm exec tsc --noEmit
git add src/test-utils
git commit -m "test(frontend): shared Map-backed storage polyfill for vitest"
```

---

### Task 2: Key-filter the two unfiltered cross-tab storage listeners

`useGateway.tsx:78-87` bumps provider state on EVERY cross-tab localStorage write; `DesktopPermissionsGate.tsx:43-51` re-reads its grant likewise. PR-2's debounced layout writes from another tab would thrash both. Filter to the desktop-permissions key (and `key === null`, which `localStorage.clear()` emits).

**Files:**
- Modify: `src/lib/desktopPermissions.ts` (add predicate after line 17)
- Modify: `src/hooks/useGateway.tsx:78-87`
- Modify: `src/components/desktop/DesktopPermissionsGate.tsx:43-51`
- Test: `src/lib/__tests__/desktopPermissionsStorageEvent.test.ts`

**Interfaces:**
- Produces: `isPermissionsStorageEvent(event: StorageEvent): boolean` exported from `src/lib/desktopPermissions.ts`.
- Consumes: `DESKTOP_PERMISSIONS_STORAGE_KEY` (`desktopPermissions.ts:16`).

- [ ] **Step 1: Write the failing test**

```ts
// src/lib/__tests__/desktopPermissionsStorageEvent.test.ts
import { describe, it, expect } from "vitest";
import {
  isPermissionsStorageEvent,
  DESKTOP_PERMISSIONS_STORAGE_KEY,
} from "../desktopPermissions";

describe("isPermissionsStorageEvent", () => {
  it("matches the permissions key", () => {
    expect(
      isPermissionsStorageEvent({ key: DESKTOP_PERMISSIONS_STORAGE_KEY } as StorageEvent),
    ).toBe(true);
  });
  it("matches key=null (localStorage.clear from another tab)", () => {
    expect(isPermissionsStorageEvent({ key: null } as StorageEvent)).toBe(true);
  });
  it("ignores unrelated keys (e.g. future workspace layout writes)", () => {
    expect(
      isPermissionsStorageEvent({ key: "isol8.workspace.layout.v1:user_x" } as StorageEvent),
    ).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/lib/__tests__/desktopPermissionsStorageEvent.test.ts --silent`
Expected: FAIL — `isPermissionsStorageEvent` is not exported

- [ ] **Step 3: Add the predicate to desktopPermissions.ts** (below the constants at line 17)

```ts
// Cross-tab storage-event filter: only permission-grant writes (or a full
// localStorage.clear, which fires with key === null) concern the listeners
// in useGateway / DesktopPermissionsGate. Unrelated keys — notably the
// frequently-written workspace layout blob — must not wake them.
export function isPermissionsStorageEvent(event: StorageEvent): boolean {
  return event.key === null || event.key === DESKTOP_PERMISSIONS_STORAGE_KEY;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run src/lib/__tests__/desktopPermissionsStorageEvent.test.ts --silent`
Expected: PASS

- [ ] **Step 5: Filter the useGateway listener**

In `src/hooks/useGateway.tsx`, replace the effect at lines 78-87:

```ts
  useEffect(() => {
    if (typeof window === "undefined") return;
    const bump = () => setDesktopPermissionRevision((value) => value + 1);
    const onStorage = (event: StorageEvent) => {
      if (isPermissionsStorageEvent(event)) bump();
    };
    window.addEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, bump);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, bump);
      window.removeEventListener("storage", onStorage);
    };
  }, []);
```

Add `isPermissionsStorageEvent` to the existing `@/lib/desktopPermissions` import in that file.

- [ ] **Step 6: Filter the DesktopPermissionsGate subscription**

In `src/components/desktop/DesktopPermissionsGate.tsx`, replace `subscribePermissionGate` (lines 43-51):

```ts
function subscribePermissionGate(onStoreChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (isPermissionsStorageEvent(event)) onStoreChange();
  };
  window.addEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, onStoreChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, onStoreChange);
    window.removeEventListener("storage", onStorage);
  };
}
```

Add `isPermissionsStorageEvent` to that file's `@/lib/desktopPermissions` import.

- [ ] **Step 7: Verify existing suites still pass, typecheck, commit**

```bash
pnpm vitest run src/lib/__tests__/desktopPermissions.test.ts --silent
pnpm vitest run src/__tests__/components/desktop/ --silent   # if the dir exists; skip if not
pnpm exec tsc --noEmit
git add src/lib/desktopPermissions.ts src/lib/__tests__/desktopPermissionsStorageEvent.test.ts src/hooks/useGateway.tsx src/components/desktop/DesktopPermissionsGate.tsx
git commit -m "fix(chat): key-filter cross-tab storage listeners to the permissions key"
```

---

### Task 3: Scope FileContentViewer's Cmd+S to its own subtree

`FileContentViewer.tsx:62-71` registers a window keydown that saves whenever `canSaveRef` is true — with two docked editors (PR-2), one Cmd+S would fire both saves. Scope: only save when focus is inside this instance.

**Deliberate behavior tightening (approved in spec §4):** today Cmd+S saves even when focus is in the chat composer; after this change it saves only when focus is within the viewer. Pressing Cmd+S elsewhere falls through to the browser default.

**Files:**
- Modify: `src/components/chat/FileContentViewer.tsx`
- Test: `src/__tests__/components/chat/FileContentViewer.cmdS.test.tsx`

**Interfaces:**
- Consumes: `FileContentViewer` props as-is (`file`, `onSave`, `isLoading`, `error`, …) — check the top of the file for the exact prop type when writing the test harness.
- Produces: no API change.

- [ ] **Step 1: Read the component's prop type** (top of `FileContentViewer.tsx`) and the render roots (loading/error/content branches at lines 73-88+) so the ref lands on every branch's root, then **write the failing test**:

```tsx
// src/__tests__/components/chat/FileContentViewer.cmdS.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { FileContentViewer } from "@/components/chat/FileContentViewer";

const file = { path: "notes.md", content: "hello", binary: false };

function pressCmdS() {
  fireEvent.keyDown(window, { key: "s", metaKey: true });
}

describe("FileContentViewer Cmd+S scoping", () => {
  it("saves when focus is inside the viewer", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { container } = render(
      <FileContentViewer file={file} onSave={onSave} isLoading={false} error={null} />,
    );
    const textarea = container.querySelector("textarea");
    // Editor may need edit mode toggled; if no textarea, focus the root.
    (textarea ?? (container.firstElementChild as HTMLElement)).focus?.();
    // Make content dirty via the textarea when present so canSave is true.
    if (textarea) fireEvent.change(textarea, { target: { value: "hello world" } });
    pressCmdS();
    expect(onSave).toHaveBeenCalledTimes(textarea ? 1 : 0);
  });

  it("does NOT save when focus is outside the viewer", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<FileContentViewer file={file} onSave={onSave} isLoading={false} error={null} />);
    const outside = document.createElement("input");
    document.body.appendChild(outside);
    outside.focus();
    pressCmdS();
    expect(onSave).not.toHaveBeenCalled();
    outside.remove();
  });
});
```

Adapt the harness details (props, edit-mode toggling) to the component's real API — read the file first; the assertion pair (inside fires, outside doesn't) is the contract.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/__tests__/components/chat/FileContentViewer.cmdS.test.tsx --silent`
Expected: the "outside" case FAILS (save fires today regardless of focus)

- [ ] **Step 3: Implement the scoping**

In `FileContentViewer.tsx`: create a root ref and gate the handler.

```ts
  const rootRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey) || e.key !== "s") return;
      if (!canSaveRef.current) return;
      // Only the instance that owns focus responds — with several viewers
      // docked side by side, one Cmd+S must not save all of them.
      if (!rootRef.current?.contains(document.activeElement)) return;
      e.preventDefault();
      handleSaveRef.current();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);
```

Attach `ref={rootRef}` to the root element of EVERY return branch that can host focus (the main content wrapper; loading/error branches have no focusable children but attaching to a shared outer wrapper is cleaner if the component structure allows one).

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run src/__tests__/components/chat/FileContentViewer.cmdS.test.tsx --silent`
Expected: PASS (both cases)

- [ ] **Step 5: Typecheck and commit**

```bash
pnpm exec tsc --noEmit
git add src/components/chat/FileContentViewer.tsx src/__tests__/components/chat/FileContentViewer.cmdS.test.tsx
git commit -m "fix(chat): scope file-editor Cmd+S to the focused viewer instance"
```

---

### Task 4: Move FileViewer's inline global <style> into a CSS file

`FileViewer.tsx:153-187` injects an unscoped `<style>` tag per instance. Move the rules verbatim to a CSS file. NOTE: `.file-viewer-panel { grid-area: viewer }` in `ChatLayout.css:12-15` stays — severing the grid coupling is PR-2.

**Files:**
- Create: `src/components/chat/FileViewer.css`
- Modify: `src/components/chat/FileViewer.tsx` (delete lines 153-187, add import)

- [ ] **Step 1: Create the CSS file** with the exact rules from the style block:

```css
/* src/components/chat/FileViewer.css — extracted from the former inline
   <style> so two docked instances don't inject duplicate global tags. */
.file-viewer-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #faf7f2;
  border-left: 1px solid #e0dbd0;
}
.file-viewer-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  height: 56px;
  border-bottom: 1px solid #e0dbd0;
  background: #faf7f2;
  flex-shrink: 0;
}
.file-viewer-body {
  display: flex;
  flex: 1;
  min-height: 0;
}
.file-viewer-tree {
  width: 220px;
  border-right: 1px solid #e0dbd0;
  flex-shrink: 0;
  overflow: hidden;
}
.file-viewer-content {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}
```

- [ ] **Step 2: Swap the component over**

In `FileViewer.tsx`: add `import "./FileViewer.css";` with the other imports; delete the entire `<style>{\`...\`}</style>` JSX block (lines 153-187).

- [ ] **Step 3: Verify build + lint + typecheck**

```bash
pnpm exec tsc --noEmit && pnpm run lint --quiet
pnpm vitest run src/__tests__/components/chat/ --silent 2>/dev/null || true  # existing chat component suites
```

Expected: clean. (Visual parity is covered by the final task's manual dev check.)

- [ ] **Step 4: Commit**

```bash
git add src/components/chat/FileViewer.css src/components/chat/FileViewer.tsx
git commit -m "refactor(chat): move FileViewer styles from inline <style> to CSS file"
```

---

### Task 5: ChatErrorCard off next/link

`ChatErrorCard.tsx:113` renders its action via `next/link` — a router navigation that bypasses the shellNav History rail and pays an RSC round-trip on the force-dynamic `/chat` route (spec §6 rule 9).

**Files:**
- Modify: `src/components/chat/ChatErrorCard.tsx`
- Test: `src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx`

**Interfaces:**
- Consumes: `handleShellLinkClick(e, url)` from `@/lib/shellNav` (signature verified: `(e: {defaultPrevented; metaKey; ctrlKey; shiftKey; altKey; button; preventDefault()}, url: string) => void`).

- [ ] **Step 1: Write the failing test**

```tsx
// src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import { ChatErrorCard } from "@/components/chat/ChatErrorCard";

afterEach(() => vi.restoreAllMocks());

describe("ChatErrorCard action link", () => {
  it("navigates via pushState on plain left-click (no router round-trip)", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="rate_limit" message="" />);
    const link = screen.getByRole("link", { name: /switch provider/i });
    expect(link).toHaveAttribute("href", "/chat?panel=llm&action=change-provider");
    fireEvent.click(link, { button: 0 });
    expect(push).toHaveBeenCalledWith(null, "", "/chat?panel=llm&action=change-provider");
  });

  it("lets modified clicks fall through to native anchor behavior", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="rate_limit" message="" />);
    fireEvent.click(screen.getByRole("link", { name: /switch provider/i }), {
      button: 0,
      metaKey: true,
    });
    expect(push).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx --silent`
Expected: FAIL — next/link's click handling doesn't call the spied pushState with these args (it goes through the router)

- [ ] **Step 3: Swap Link for a shellNav anchor**

In `ChatErrorCard.tsx`: remove the `import Link from "next/link";` line; add `import { handleShellLinkClick } from "@/lib/shellNav";`. Replace the action button JSX (lines 106-115):

```tsx
            {action && (
              <Button
                asChild
                size="sm"
                variant="outline"
                className="h-7 px-3 text-xs border-[#c0392b] text-[#7a2018] hover:bg-[#fbd6d2]"
              >
                <a href={action.href} onClick={(e) => handleShellLinkClick(e, action.href)}>
                  {action.label}
                </a>
              </Button>
            )}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx --silent`
Expected: PASS. Also run any existing ChatErrorCard suite: `pnpm vitest run src/__tests__/components/chat/ --silent` (single directory, acceptable size — if it OOMs, run only files matching ChatErrorCard).

- [ ] **Step 5: Typecheck and commit**

```bash
pnpm exec tsc --noEmit
git add src/components/chat/ChatErrorCard.tsx src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx
git commit -m "fix(chat): route ChatErrorCard action through shellNav instead of next/link"
```

---

### Task 6: Chat history revalidates on mount (transcript-loss fix)

`useAgentChat.ts:727-733`: the history effect short-circuits whenever `_messageCache` has the key, so a run that completes while the hook is unmounted never appears until a full reload (spec §4). Fix: cached transcript paints instantly, but history is still fetched once per mount and the server result replaces the cache.

**Files:**
- Modify: `src/hooks/useAgentChat.ts:727-751`
- Test: `src/hooks/__tests__/useAgentChat.historyRevalidate.test.tsx`

**Interfaces:**
- Consumes: the existing test harness pattern — READ `src/hooks/__tests__/useAgentChat.cacheIntegrity.test.tsx` FIRST and copy its mock/render scaffolding exactly (it already fakes the gateway and drives `chat.history`).

- [ ] **Step 1: Study the harness, then write the failing test**

Scenario (adapt to the harness's helpers — the two assertions are the contract):

```tsx
// src/hooks/__tests__/useAgentChat.historyRevalidate.test.tsx
// Harness copied from useAgentChat.cacheIntegrity.test.tsx.
//
// Scenario 1 ("stale cache refreshes"): mount the hook once so history
// loads and _messageCache holds [msgA]; unmount; make the mocked
// chat.history now return [msgA, msgB]; remount with the same
// agent/session. EXPECT: chat.history is called again on the remount,
// and the transcript ends as [msgA, msgB].
//
// Scenario 2 ("no loading flash with cache"): on that same remount,
// the history load state must never leave "done" (cached transcript
// paints; the refetch is a background revalidation).
```

Write it with the harness's real helper names; assert `sendReq`/`chat.history` call count === 2 across the two mounts, final messages contain msgB, and `historyLoadState` (however the harness exposes it — via hook return) stays `"done"` during the remount fetch.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/hooks/__tests__/useAgentChat.historyRevalidate.test.tsx --silent`
Expected: FAIL — chat.history called once (second mount short-circuits on the cache)

- [ ] **Step 3: Implement the revalidation**

In `useAgentChat.ts`, replace lines 727-751 (the effect body up to the `projected.length === 0` branch) with:

```ts
  useEffect(() => {
    if (!agentId || !cacheKey || !isConnected) return;
    if (historyLoadedRef.current.has(cacheKey)) return;
    // Cached transcript paints instantly, but a run that completed while
    // this hook was unmounted only exists server-side — so always fetch
    // once per mount and let the server result replace the cache.
    const hasCache = _messageCache.has(cacheKey);
    const sessionKey = buildChatSessionKey(agentId, sessionName, activeSessionId);
    if (!hasCache) {
      Promise.resolve().then(() => setHistoryLoadState("loading"));
    }
    sendReq("chat.history", { sessionKey, limit: 200 })
      .then((result: unknown) => {
        const historyResult = result as { messages?: HistoryMessage[]; truncated?: boolean };
        historyLoadedRef.current.add(cacheKey);
        if (historyResult?.truncated === true) {
          _truncatedHistory.add(cacheKey);
        } else {
          _truncatedHistory.delete(cacheKey);
        }
        setHistoryLoadState("done");
        const raw = Array.isArray(historyResult?.messages) ? historyResult.messages : [];
        const projected = projectChatHistoryMessages(raw);
        if (projected.length === 0) {
          // Server has nothing: only flag bootstrap when we ALSO have no
          // cache — an existing cached transcript must not regress to the
          // bootstrap suggestion because of an empty/partial server read.
          if (!hasCache) {
            _needsBootstrap.add(cacheKey);
          }
          return;
        }
```

Keep the remainder of the `.then` (the `agentIdRef` guard, `_messageCache.set`, `setChatMessages` under the `activeSessionIdRef` check) and the existing `.catch` exactly as they are.

- [ ] **Step 4: Run the new test + the neighboring history/cache suites**

```bash
pnpm vitest run src/hooks/__tests__/useAgentChat.historyRevalidate.test.tsx --silent
pnpm vitest run src/hooks/__tests__/useAgentChat.cacheIntegrity.test.tsx --silent
pnpm vitest run src/hooks/__tests__/useAgentChat.sessions.test.tsx --silent
pnpm vitest run src/hooks/__tests__/useAgentChat.sessionRouting.test.tsx --silent
```

Expected: all PASS. If cacheIntegrity pinned the old short-circuit, update that expectation deliberately (it is the bug).

- [ ] **Step 5: Typecheck and commit**

```bash
pnpm exec tsc --noEmit
git add src/hooks/useAgentChat.ts src/hooks/__tests__/useAgentChat.historyRevalidate.test.tsx src/hooks/__tests__/useAgentChat.cacheIntegrity.test.tsx
git commit -m "fix(chat): revalidate chat history on mount so runs finished while unmounted appear"
```

---

### Task 7: Extract ShellServices hooks from ChatLayout

Pure moves into `src/components/chat/shell/`. ChatLayout keeps thin call sites; PR-2's WorkspaceShell consumes the same hooks. The GetStarted floating guide RENDERING stays in ChatLayout (PR-2 moves it).

**Files:**
- Create: `src/components/chat/shell/useUserSyncOnce.ts` (moves ChatLayout.tsx:187-247)
- Create: `src/components/chat/shell/useCheckoutReturn.ts` (moves :119-132 state + :258-309 effects, incl. the ?credits strip)
- Create: `src/components/chat/shell/useSellerPublishedAgents.ts` (moves :324-350)
- Create: `src/components/chat/shell/useGetStartedDerived.ts` (moves :371-477, minus the `container`/`chatReady` derivation which stays in ChatLayout)
- Modify: `src/components/chat/ChatLayout.tsx` (replace moved blocks with hook calls; prune now-unused imports)
- Test: `src/components/chat/shell/__tests__/useUserSyncOnce.test.tsx`

**Interfaces (produced — PR-2 depends on these exact signatures):**

```ts
export function useUserSyncOnce(enabled: boolean): void;
export function useCheckoutReturn(): { showSubscriptionSuccess: boolean };
export function useSellerPublishedAgents(): Set<string>;
export interface GetStartedDerivedSignals { chat: boolean; hire: boolean; channel: boolean; task: boolean; }
export function useGetStartedDerived(chatReady: boolean): GetStartedDerivedSignals;
```

Move rules (apply to every hook): copy the block verbatim including its explanatory comments; move exactly the imports the block uses (`useSWR`, `myListings`, `teamsQueryKeys`, `ChannelLinksResponse`, `replaceSearchParams`, etc.) from ChatLayout into the new file; each hook calls its own `useApi()` / `useSearchParams()` / `useBilling()` / `useTeamsApi()` / `useAuth()` / `useUser()` — all are SWR/context-backed, so double invocation with ChatLayout dedupes and is behavior-neutral.

- [ ] **Step 1: Write the failing test for useUserSyncOnce**

```tsx
// src/components/chat/shell/__tests__/useUserSyncOnce.test.tsx
import { describe, it, expect, vi, beforeEach, afterAll } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";

const syncUser = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ syncUser }) }));

import { useUserSyncOnce } from "../useUserSyncOnce";

const restore = installStoragePolyfill();
afterAll(restore);
beforeEach(() => {
  syncUser.mockReset();
  window.sessionStorage.clear();
});

describe("useUserSyncOnce", () => {
  it("does nothing while disabled", () => {
    renderHook(() => useUserSyncOnce(false));
    expect(syncUser).not.toHaveBeenCalled();
  });

  it("syncs exactly once when enabled, across rerenders, and sets the session marker", async () => {
    syncUser.mockResolvedValue(undefined);
    const { rerender } = renderHook(({ on }) => useUserSyncOnce(on), {
      initialProps: { on: true },
    });
    rerender({ on: true });
    rerender({ on: true });
    await waitFor(() => expect(window.sessionStorage.getItem("isol8:user-synced")).toBe("1"));
    expect(syncUser).toHaveBeenCalledTimes(1);
  });

  it("skips when the session marker is already set", () => {
    window.sessionStorage.setItem("isol8:user-synced", "1");
    renderHook(() => useUserSyncOnce(true));
    expect(syncUser).not.toHaveBeenCalled();
  });

  it("releases the latch on failure so a remount can retry", async () => {
    syncUser.mockRejectedValueOnce(new Error("boom"));
    const consoleErr = vi.spyOn(console, "error").mockImplementation(() => {});
    const first = renderHook(() => useUserSyncOnce(true));
    await waitFor(() => expect(syncUser).toHaveBeenCalledTimes(1));
    expect(window.sessionStorage.getItem("isol8:user-synced")).toBeNull();
    first.unmount();
    syncUser.mockResolvedValue(undefined);
    renderHook(() => useUserSyncOnce(true));
    await waitFor(() => expect(syncUser).toHaveBeenCalledTimes(2));
    consoleErr.mockRestore();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/components/chat/shell/__tests__/useUserSyncOnce.test.tsx --silent`
Expected: FAIL — module not found

- [ ] **Step 3: Create useUserSyncOnce.ts**

```ts
// src/components/chat/shell/useUserSyncOnce.ts
"use client";

import { useEffect, useRef } from "react";
import { useApi } from "@/lib/api";

// [Move the full rationale comment from ChatLayout.tsx:201-217 here verbatim.]
export function useUserSyncOnce(enabled: boolean): void {
  const api = useApi();
  const syncStartedRef = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    if (syncStartedRef.current) return;
    const syncedKey = "isol8:user-synced";
    try {
      if (sessionStorage.getItem(syncedKey)) return;
    } catch {
      // sessionStorage unavailable (privacy mode) — fall through and sync.
    }
    syncStartedRef.current = true;
    api
      .syncUser()
      .then(() => {
        try {
          sessionStorage.setItem(syncedKey, "1");
        } catch {
          // best effort
        }
      })
      .catch((err: unknown) => {
        syncStartedRef.current = false;
        console.error("User sync failed:", err);
      });
  }, [enabled, api]);
}
```

(`enabled` is `isSignedIn && onboardingPhase === "ready"` at the call site — same guard set as the original's early returns.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run src/components/chat/shell/__tests__/useUserSyncOnce.test.tsx --silent`
Expected: PASS

- [ ] **Step 5: Create the three remaining hooks as verbatim moves**

`useCheckoutReturn.ts` — move state initializers (ChatLayout:128-132) + the three effects (:264-275, :283-289, :301-309) and their comments:

```ts
// src/components/chat/shell/useCheckoutReturn.ts
"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useBilling } from "@/hooks/useBilling";
import { replaceSearchParams } from "@/lib/shellNav";

// [Move the comments from ChatLayout.tsx:120-127, 258-263, 277-282, 291-300 verbatim.]
export function useCheckoutReturn(): { showSubscriptionSuccess: boolean } {
  const searchParams = useSearchParams();
  const { refresh: refreshBilling, isSubscribed } = useBilling();

  const cameFromCheckout =
    searchParams.get("subscription") === "success" ||
    searchParams.get("checkout") === "success";
  const [showSubscriptionSuccess, setShowSubscriptionSuccess] = useState(() => cameFromCheckout);
  const [pollingForWebhook, setPollingForWebhook] = useState(() => cameFromCheckout);

  useEffect(() => {
    if (!showSubscriptionSuccess) return;
    refreshBilling();
    const params = new URLSearchParams(searchParams.toString());
    params.delete("checkout");
    params.delete("subscription");
    replaceSearchParams(params);
    const dismissTimer = setTimeout(() => setShowSubscriptionSuccess(false), 5000);
    return () => clearTimeout(dismissTimer);
  }, [showSubscriptionSuccess, refreshBilling, searchParams]);

  const cameFromCreditsCheckout = searchParams.get("credits") !== null;
  useEffect(() => {
    if (!cameFromCreditsCheckout) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("credits");
    replaceSearchParams(params);
  }, [cameFromCreditsCheckout, searchParams]);

  useEffect(() => {
    if (!pollingForWebhook || isSubscribed) return;
    const pollInterval = setInterval(() => refreshBilling(), 2000);
    const giveUpTimer = setTimeout(() => setPollingForWebhook(false), 30_000);
    return () => {
      clearInterval(pollInterval);
      clearTimeout(giveUpTimer);
    };
  }, [pollingForWebhook, isSubscribed, refreshBilling]);

  return { showSubscriptionSuccess };
}
```

`useSellerPublishedAgents.ts` — move ChatLayout:324-350 (the `useSWR(["seller-listings", …])` read + `publishedAgentIds` memo) into a hook returning the `Set<string>`; it calls `useUser()` + `useAuth()` for `user?.id`/`getToken` and imports `myListings` from wherever ChatLayout imports it (check the import list at the top of ChatLayout).

`useGetStartedDerived.ts` — move ChatLayout:373-477 verbatim (channels read, inbox `all` read via `useTeamsApi().read`, installed-templates read, `sessions.list` RPC, and the four booleans + memo), taking `chatReady: boolean` as the parameter and returning the `GetStartedDerivedSignals` object. The `useProvisioningState()`/`chatReady` derivation (:371-372) STAYS in ChatLayout (it uses `container` elsewhere).

- [ ] **Step 6: Swap ChatLayout over to the hooks**

In `ChatLayout.tsx`:
- Delete the moved blocks (state at :128-132; `syncStartedRef` at :190; effects at :218-247, :264-309; SWR read + memo at :333-350; derivation block at :373-477).
- At the top of the component body add:

```ts
  useUserSyncOnce(Boolean(isSignedIn) && onboardingPhase === "ready");
  const { showSubscriptionSuccess } = useCheckoutReturn();
  const publishedAgentIds = useSellerPublishedAgents();
  const getStartedDerived = useGetStartedDerived(chatReady);
```

placing each call where its former block's outputs are first needed (the `chatReady` derivation at :371-372 must stay ABOVE the `useGetStartedDerived` call).
- Imports: add the four hook imports from `./shell/...`; remove imports that became unused (`myListings`, `teamsQueryKeys`, `ChannelLinksResponse`, possibly `useSWR` — let eslint's no-unused-vars drive the exact list).
- `dismissSubscriptionSuccess` does not exist today (banner auto-dismisses) — confirm nothing else referenced `setShowSubscriptionSuccess`/`setPollingForWebhook` outside the moved effects (grep the file; if the JSX renders a manual dismiss button, extend the hook to also return a `dismiss` callback and wire it — keep behavior identical either way).

- [ ] **Step 7: Verify — existing suites, typecheck, lint, build**

```bash
pnpm vitest run src/__tests__/components/chat/ --silent
pnpm vitest run src/components/chat/shell/__tests__/useUserSyncOnce.test.tsx --silent
pnpm exec tsc --noEmit && pnpm run lint --quiet
pnpm run build
```

Expected: all clean. The build is the real gate that no import was stranded.

- [ ] **Step 8: Commit**

```bash
git add src/components/chat/shell src/components/chat/ChatLayout.tsx
git commit -m "refactor(chat): extract shell services from ChatLayout into reusable hooks"
```

---

### Task 8: Hoist ExecApprovalsProvider from ChatLayout to page.tsx

`useAgentChat` silently no-ops without this provider; PR-2 mounts chat panels outside ChatLayout's subtree, so the provider must live at the page level. Behavior-neutral now (all consumers remain inside the moved wrapper).

**Files:**
- Modify: `src/app/chat/page.tsx` (wrap ChatLayout's subtree)
- Modify: `src/components/chat/ChatLayout.tsx` (unwrap)

- [ ] **Step 1: Move the provider**

In `ChatLayout.tsx`: remove the `<ExecApprovalsProvider>` open/close tags around the DesktopPermissionsGate subtree (currently at :508 — DesktopPermissionsGate becomes the outermost element) and delete its import.

In `src/app/chat/page.tsx`: import `ExecApprovalsProvider` from `@/hooks/useExecApprovals` (copy the exact import specifier ChatLayout used), then wrap ChatLayout — inside `GatewayProvider`, leaving MarketplaceDrawer/MarketplaceToast/RunNotifications as siblings outside it:

```tsx
      <GatewayProvider>
        <ExecApprovalsProvider>
          <ChatLayout ...>...</ChatLayout>
        </ExecApprovalsProvider>
        <MarketplaceDrawer />
        <MarketplaceToast />
        <RunNotifications ... />
      </GatewayProvider>
```

(Keep the existing children/props of ChatLayout untouched — only the wrapper moves.)

- [ ] **Step 2: Verify + commit**

```bash
pnpm exec tsc --noEmit && pnpm run lint --quiet && pnpm run build
git add src/app/chat/page.tsx src/components/chat/ChatLayout.tsx
git commit -m "refactor(chat): hoist ExecApprovalsProvider to the page shell"
```

---

### Task 9: Final gates + stop for PR approval

- [ ] **Step 1: Run the full local gate set**

```bash
pnpm exec tsc --noEmit
pnpm run lint
pnpm run build
pnpm vitest run src/hooks/__tests__/useAgentChat.historyRevalidate.test.tsx --silent
pnpm vitest run src/components/chat/shell/__tests__/useUserSyncOnce.test.tsx --silent
pnpm vitest run src/__tests__/components/chat/ --silent
```

Expected: all green. (Never run the whole vitest suite.)

- [ ] **Step 2: Manual dev sanity list** (run `pnpm run dev`, sign in with a personal dev account): chat loads and streams; file viewer opens/closes with identical styling; Cmd+S saves in the editor; a rate-limit ChatErrorCard's "Switch provider" opens the LLM panel without a page reload; checkout-return banner logic untouched (URL param strip on ?checkout=success).

- [ ] **Step 3: STOP — request user approval to push and open the PR**

Do NOT push. Summarize the diff for the user and ask to push `feat/dockable-workspace` and open the PR titled `refactor(chat): dockable-workspace PR-1 — shell extractions + singleton-hazard fixes`. PR body: summary of the 8 changes, note the deliberate Cmd+S scoping change, link the spec, and end with the standard generated-with footer. Codex will review; dedupe its re-flags.

---

## Self-review notes

- Spec coverage (PR-1 items from §9): ShellServices extraction (T7), ExecApprovals hoist (T8), storage key filters (T2), transcript fix (T6), FileViewer style + Cmd+S (T3/T4), ChatErrorCard link (T5), test-polyfill helper (T1). Teams-provider hoist is PR-2 (needs the shell); `grid-area` severing is PR-2; both noted in-task.
- Type consistency: `GetStartedDerivedSignals` matches ChatLayout's current `getStartedDerived` memo shape `{chat, hire, channel, task}`; `useCheckoutReturn` returns only what ChatLayout's JSX consumes.
- Known adaptation points (deliberate, not placeholders): T3/T6 test harness details follow existing suites the implementer must read first; T7 Step 6 lets eslint drive the pruned import list.
