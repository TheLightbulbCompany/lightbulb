# Dockable Workspace PR-2: The Dockview Shell — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the desktop `/chat` shell with a dockview-react v7 workspace — panel registry, localStorage layout persistence, URL-as-command-channel reconciler, one chat panel per agent, Mission Control default — while `ChatLayout` survives unchanged as the mobile (<768px) shell.

**Architecture:** Pure foundation modules first (registry, storage, URL channel, controller store — all unit-testable without dockview), then panel wrappers, then the `WorkspaceShell` that assembles them, then the `page.tsx` desktop/mobile branch. Panels stop reading `useSearchParams`; both shells feed them params as props (mobile derives from URL at the page, desktop from dockview panel params). Spec: `docs/superpowers/specs/2026-07-11-dockable-workspace-design.md`.

**Tech Stack:** dockview-react 7.0.2 (new dep), Next.js 16.1.1, React 19.2.3 (React Compiler ON), SWR, useSyncExternalStore module stores, vitest + @testing-library/react.

## Global Constraints

- Worktree `.claude/worktrees/dockable-workspace`, branch `feat/dockable-workspace-shell` (off origin/main = post-PR-1). All paths relative to `apps/frontend/`.
- **NEVER run the full frontend vitest suite** (RAM). Single-file runs only: `pnpm vitest run <path> --silent`. Run `pnpm exec tsc --noEmit` after every task (CI typechecks tests). `pnpm run build` at integration tasks.
- **Every dockview import — types AND CSS — comes from `'dockview-react'`** (`dockview`/`dockview-core` are transitive, not hoisted under pnpm; importing them fails). CSS: `import "dockview-react/dist/styles/dockview.css"`.
- **v7 theme is an OBJECT prop**, not a className. `components`/`tabComponents` maps must be module-scope (identity-watched).
- Chat panels always `renderer: "always"` (default `onlyWhenVisible` detaches hidden DOM → MessageList scroll death; invisible to tests).
- `disableFloatingGroups: true`; sanitizer strips `floatingGroups`/`popoutGroups`/`edgeGroups` from persisted blobs.
- URL model (spec §6): inbound reconciler executes `?panel` commands as open-or-focus; **panel focus writes ZERO history entries** (invariant test); only explicit navigations push; closing the URL-named panel does one `replaceState` clearing its full durable param set; one-shots (`new`, `newIssue`, `action`) are stripped at copy time and never serialized into the layout blob; `checkout`/`subscription`/`credits`/`provider` are NOT commands (billing returns — `useCheckoutReturn` owns them).
- Layout blob key: `isol8.workspace.layout.v1:${orgId ?? userId}`; value `{ version: 1, savedAt, layout }`; writes debounced ~500 ms, flushed on `pagehide` + `visibilitychange:hidden` + dock unmount; **never `localStorage.clear()`** (key===null wakes the permissions listeners); all reads/writes try/catch-swallowed.
- The dock mounts only behind `useOnboardingGate() === "ready"` AND inside ProvisioningStepper's ready gate — the blob key's scope-finality and Teams 402-upstream guarantees depend on this ordering. `useOnboardingGate` has router side effects — mount it exactly ONCE per shell.
- ChatLayout stays the mobile shell: every edit to shared components (panels, ChatInput, RunNotifications) must keep the mobile path compiling and behaving. Desktop = `matchMedia("(min-width: 769px)")` (CSS mobile block is `max-width: 768px`).
- New analytics events must join the `AnalyticsEvent` union in `src/lib/analytics.ts` (~line 62) or tsc fails.
- Do NOT touch `src/components/teams/inbox/hooks/*` (concurrent human work). `InboxPage.tsx` edits ARE in scope (the human's changes landed).
- No push without explicit user approval (final task stops).
- Line refs verified against origin/main @ 72f4da94f — re-locate by quoted code if drifted.

## File Structure

```
src/components/workspace/            NEW — everything dock-specific
├── panelRegistry.ts                 panel keys → component names, ids, aliases, param classes
├── layoutStorage.ts                 blob read/validate/sanitize/write/flush
├── urlChannel.ts                    searchParams ⇄ WorkspaceCommand, clear-on-close sets
├── workspaceController.ts           module store: DockviewApi holder + open/focus API + reactive state
├── buildDefaultLayout.ts            Mission Control
├── WorkspaceShell.tsx               gate → chrome → providers → DockviewReact + reconciler + persistence
├── WorkspaceSidebar.tsx             desktop sidebar chrome (roster, CompanyNav, gallery, footer)
├── WorkspaceHeader.tsx              desktop top bar (file button, support, UserButton)
├── panels/ChatPanel.tsx             per-agent chat panel (sentinel, agent-gone, action cluster)
├── panels/TeamsDockPanel.tsx        teams list/detail panels host (status gate + TeamsPanelRouter)
├── panels/ControlDockPanel.tsx      legacy control keys host (ControlPanelRouter)
├── panels/FileViewerDockPanel.tsx   file viewer host
├── panels/MissingPanel.tsx          placeholder for unknown restored panels
└── __tests__/                       colocated tests
src/hooks/useIsDesktop.ts            NEW — SSR-safe matchMedia store
src/hooks/useChannelLinks.ts         NEW — carry-forward hoist (3 private copies today)
src/lib/desktopPermissionsSubscribe.ts NEW — shared filtered subscribe helper
```

---

### Task 1: Add dockview-react 7.0.2 + compile probe

**Files:**
- Modify: `apps/frontend/package.json` (via pnpm)
- Create: `src/components/workspace/__tests__/dockviewImports.test.ts`

**Interfaces:**
- Produces: the `dockview-react@7.0.2` dependency every later task imports.

- [ ] **Step 1: Install (from the REPO ROOT, not apps/frontend)**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/dockable-workspace
pnpm add dockview-react@7.0.2 --filter @isol8/frontend
```

Expected: `apps/frontend/package.json` gains `"dockview-react": "7.0.2"` (exact pin, no caret — edit the entry if pnpm added `^`). Lockfile updates.

- [ ] **Step 2: Write the import-surface probe test**

```ts
// src/components/workspace/__tests__/dockviewImports.test.ts
// Pins the exact import surface the workspace code relies on. If a future
// dockview bump renames any of these, this file fails before anything else.
import { describe, it, expect } from "vitest";
import {
  DockviewReact,
  themeLight,
  type DockviewReadyEvent,
  type DockviewApi,
  type IDockviewPanelProps,
  type SerializedDockview,
  type AddPanelOptions,
  type DockviewTheme,
} from "dockview-react";

describe("dockview-react import surface", () => {
  it("exports the component, theme object, and types we use", () => {
    expect(DockviewReact).toBeDefined();
    expect(themeLight.name).toBeTypeOf("string");
    expect(themeLight.className).toBeTypeOf("string");
    const _t: DockviewTheme = { ...themeLight, name: "isol8" };
    expect(_t.name).toBe("isol8");
    // Type-only names — reference them so tsc checks they exist:
    const _u:
      | DockviewReadyEvent
      | DockviewApi
      | IDockviewPanelProps
      | SerializedDockview
      | AddPanelOptions
      | null = null;
    expect(_u).toBeNull();
  });
});
```

- [ ] **Step 3: Run it + typecheck**

Run: `cd apps/frontend && pnpm vitest run src/components/workspace/__tests__/dockviewImports.test.ts --silent && pnpm exec tsc --noEmit`
Expected: PASS, tsc clean. If the resolved version differs from 7.0.2, STOP and report (API facts were verified against 7.0.2 exactly).

- [ ] **Step 4: Commit**

```bash
git add package.json ../../pnpm-lock.yaml src/components/workspace/__tests__/dockviewImports.test.ts
git commit -m "feat(workspace): add dockview-react 7.0.2 with import-surface probe"
```

---

### Task 2: useIsDesktop hook

**Files:**
- Create: `src/hooks/useIsDesktop.ts`
- Test: `src/hooks/__tests__/useIsDesktop.test.tsx`

**Interfaces:**
- Produces: `useIsDesktop(): boolean` — true at `(min-width: 769px)`; SSR snapshot false (mobile-first: the server renders the mobile shell, the desktop swaps in on hydration — matches the existing CSS breakpoint at ChatLayout.css `@media (max-width: 768px)`).

- [ ] **Step 1: Write the failing test**

```tsx
// src/hooks/__tests__/useIsDesktop.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useIsDesktop } from "../useIsDesktop";

type Listener = (e: { matches: boolean }) => void;

function installMatchMedia(initialMatches: boolean) {
  const listeners = new Set<Listener>();
  const mql = {
    matches: initialMatches,
    media: "(min-width: 769px)",
    addEventListener: (_: string, cb: Listener) => listeners.add(cb),
    removeEventListener: (_: string, cb: Listener) => listeners.delete(cb),
  };
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue(mql));
  return {
    fire(matches: boolean) {
      mql.matches = matches;
      listeners.forEach((cb) => cb({ matches }));
    },
  };
}

beforeEach(() => vi.unstubAllGlobals());

describe("useIsDesktop", () => {
  it("reflects the media query and reacts to changes", () => {
    const mm = installMatchMedia(true);
    const { result } = renderHook(() => useIsDesktop());
    expect(result.current).toBe(true);
    act(() => mm.fire(false));
    expect(result.current).toBe(false);
  });

  it("queries exactly (min-width: 769px)", () => {
    installMatchMedia(false);
    renderHook(() => useIsDesktop());
    expect(window.matchMedia).toHaveBeenCalledWith("(min-width: 769px)");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/hooks/__tests__/useIsDesktop.test.tsx --silent`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```ts
// src/hooks/useIsDesktop.ts
"use client";

import { useSyncExternalStore } from "react";

const QUERY = "(min-width: 769px)";
// The chat shell's CSS mobile block is @media (max-width: 768px)
// (ChatLayout.css) — this is its exact complement, so the JS branch and the
// CSS branch can never disagree at any viewport width.

function subscribe(onStoreChange: () => void): () => void {
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", onStoreChange);
  return () => mql.removeEventListener("change", onStoreChange);
}

function getSnapshot(): boolean {
  return window.matchMedia(QUERY).matches;
}

export function useIsDesktop(): boolean {
  // Server snapshot false: SSR renders the mobile shell; desktop swaps in on
  // hydration. Chat state survives the swap via module stores.
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
```

- [ ] **Step 4: Run test + typecheck, commit**

```bash
pnpm vitest run src/hooks/__tests__/useIsDesktop.test.tsx --silent && pnpm exec tsc --noEmit
git add src/hooks/useIsDesktop.ts src/hooks/__tests__/useIsDesktop.test.tsx
git commit -m "feat(workspace): SSR-safe useIsDesktop breakpoint hook"
```

---

### Task 3: Panel registry

**Files:**
- Create: `src/components/workspace/panelRegistry.ts`
- Test: `src/components/workspace/__tests__/panelRegistry.test.ts`

**Interfaces:**
- Produces (consumed by urlChannel, controller, shell, storage sanitizer):

```ts
export type PanelKind = "chat" | "teams" | "control" | "file-viewer" | "missing";
export interface PanelDescriptor {
  panelId: string;        // deterministic dockview panel id
  component: PanelKind;   // key into the components map
  title: string;
  params: Record<string, string>;
  renderer?: "always";
}
export const PANEL_COMPONENT_KEYS: ReadonlySet<string>;         // valid contentComponent names
export const RETIRED_PANEL_ALIASES: Readonly<Record<string, string>>;
export const ONE_SHOT_PARAMS: readonly ["new", "newIssue", "action"];
export const NON_COMMAND_PARAMS: readonly ["checkout", "subscription", "credits", "provider"];
export function resolvePanelAlias(panelKey: string): string;
export function chatPanelId(agentId: string): string;           // `chat:${agentId}`
export const DEFAULT_AGENT_SENTINEL = "__default__";
export function describePanel(panelKey: string, params: Record<string, string>): PanelDescriptor | null;
export function durableParamsFor(panelId: string): string[];    // clear-on-close set
```

- [ ] **Step 1: Write the failing test**

```ts
// src/components/workspace/__tests__/panelRegistry.test.ts
import { describe, it, expect } from "vitest";
import {
  resolvePanelAlias,
  chatPanelId,
  describePanel,
  durableParamsFor,
  DEFAULT_AGENT_SENTINEL,
  PANEL_COMPONENT_KEYS,
} from "../panelRegistry";

describe("panelRegistry", () => {
  it("resolves retired aliases (both routers' maps, merged)", () => {
    expect(resolvePanelAlias("credits")).toBe("llm");
    expect(resolvePanelAlias("teams-approvals")).toBe("teams-inbox");
    expect(resolvePanelAlias("teams-members")).toBe("teams-settings");
    expect(resolvePanelAlias("teams-skills")).toBe("teams-dashboard");
    expect(resolvePanelAlias("teams-inbox")).toBe("teams-inbox");
  });

  it("builds deterministic chat panel ids", () => {
    expect(chatPanelId("agent-123")).toBe("chat:agent-123");
    expect(chatPanelId(DEFAULT_AGENT_SENTINEL)).toBe("chat:__default__");
  });

  it("describes teams list panels as singletons", () => {
    const d = describePanel("teams-inbox", {});
    expect(d).toEqual({
      panelId: "teams-inbox",
      component: "teams",
      title: "Inbox",
      params: { teamsKey: "inbox" },
    });
  });

  it("describes teams detail panels keyed per detailId", () => {
    const d = describePanel("teams-issue-detail", { detailId: "ADM-1" });
    expect(d?.panelId).toBe("teams-issue-detail:ADM-1");
    expect(d?.component).toBe("teams");
    expect(d?.params).toEqual({ teamsKey: "issue-detail", detailId: "ADM-1" });
  });

  it("returns null for a detail key without detailId", () => {
    expect(describePanel("teams-issue-detail", {})).toBeNull();
  });

  it("describes the agent inspector with durable params only", () => {
    const d = describePanel("agent-inspector", {
      agent: "a-1",
      inspectorTab: "channels",
      new: "task", // one-shot — must NOT survive into panel params
    });
    expect(d?.panelId).toBe("agent-inspector");
    expect(d?.component).toBe("control");
    expect(d?.params).toEqual({
      controlKey: "agent-inspector",
      agentId: "a-1",
      initialTab: "channels",
    });
  });

  it("describes legacy control keys (grammar parity for existing links)", () => {
    const d = describePanel("llm", {});
    expect(d).toEqual({
      panelId: "control:llm",
      component: "control",
      title: "Models & Providers",
      params: { controlKey: "llm" },
    });
  });

  it("describes chat panels with renderer always", () => {
    const d = describePanel("chat", { agent: "a-9" });
    expect(d).toEqual({
      panelId: "chat:a-9",
      component: "chat",
      title: "Chat",
      params: { agentId: "a-9" },
      renderer: "always",
    });
  });

  it("names durable params per panel id for clear-on-close", () => {
    expect(durableParamsFor("teams-issue-detail:ADM-1")).toEqual(["panel", "detailId"]);
    expect(durableParamsFor("agent-inspector")).toEqual(["panel", "agent", "inspectorTab"]);
    expect(durableParamsFor("teams-issues")).toEqual(["panel", "agent"]);
    expect(durableParamsFor("teams-inbox")).toEqual(["panel"]);
    expect(durableParamsFor("chat:a-1")).toEqual([]); // chat is never URL-named
  });

  it("registers exactly the component keys the shell's components map will have", () => {
    expect([...PANEL_COMPONENT_KEYS].sort()).toEqual(
      ["chat", "control", "file-viewer", "missing", "teams"].sort(),
    );
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/components/workspace/__tests__/panelRegistry.test.ts --silent`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```ts
// src/components/workspace/panelRegistry.ts
// Single source of panel identity for the dockview workspace. The URL
// reconciler, the controller, the persistence sanitizer, and the shell's
// components map all derive from this file — nothing else may invent a
// panel id or component name.

export type PanelKind = "chat" | "teams" | "control" | "file-viewer" | "missing";

export interface PanelDescriptor {
  panelId: string;
  component: PanelKind;
  title: string;
  params: Record<string, string>;
  renderer?: "always";
}

export const DEFAULT_AGENT_SENTINEL = "__default__";

// Merged from ControlPanelRouter (credits→llm) and TeamsPanelRouter
// (RETIRED_PANEL_ALIASES) — applied replace-only in the URL resolver so a
// Back landing on a retired key canonicalizes in place instead of pushing
// (the old ControlPanelRouter remap pushed, trapping Back on ?panel=credits).
export const RETIRED_PANEL_ALIASES: Readonly<Record<string, string>> = {
  credits: "llm",
  "teams-approvals": "teams-inbox",
  "teams-members": "teams-settings",
  "teams-skills": "teams-dashboard",
};

// One-shot intents: copied to the target panel's open params, immediately
// stripped from the URL, cleared after consumption, excluded from the
// persisted layout (dockview serializes panel params — an unstripped intent
// would re-fire its dialog on every restore).
export const ONE_SHOT_PARAMS = ["new", "newIssue", "action"] as const;

// Billing-return params: not commands. useCheckoutReturn owns their strips.
export const NON_COMMAND_PARAMS = ["checkout", "subscription", "credits", "provider"] as const;

const TEAMS_LIST_KEYS: Readonly<Record<string, string>> = {
  dashboard: "Dashboard",
  agents: "Agents",
  "org-chart": "Org Chart",
  inbox: "Inbox",
  issues: "Tasks",
  routines: "Routines",
  goals: "Goals",
  projects: "Projects",
  activity: "Activity",
  costs: "Costs",
  settings: "Team Settings",
};

const TEAMS_DETAIL_KEYS: Readonly<Record<string, string>> = {
  "issue-detail": "Task",
  "routine-detail": "Routine",
  "project-detail": "Project",
  "goal-detail": "Goal",
};

// Legacy control keys kept for URL grammar parity (ChatErrorCard links to
// ?panel=llm; AgentManageSection to ?panel=agents&agent=…). They open as
// dock panels but are NOT in the sidebar — /settings remains their home.
const CONTROL_KEYS: Readonly<Record<string, string>> = {
  overview: "Overview",
  sessions: "Sessions",
  channels: "Channels",
  usage: "Usage",
  agents: "Agents",
  skills: "Skills",
  llm: "Models & Providers",
  integrations: "Integrations",
  dreams: "Dreams",
  "skill-proposals": "Skill Proposals",
};

export const PANEL_COMPONENT_KEYS: ReadonlySet<string> = new Set([
  "chat",
  "teams",
  "control",
  "file-viewer",
  "missing",
]);

export function resolvePanelAlias(panelKey: string): string {
  return RETIRED_PANEL_ALIASES[panelKey] ?? panelKey;
}

export function chatPanelId(agentId: string): string {
  return `chat:${agentId}`;
}

export function describePanel(
  panelKey: string,
  params: Record<string, string>,
): PanelDescriptor | null {
  const key = resolvePanelAlias(panelKey);

  if (key === "chat") {
    const agentId = params.agent ?? params.agentId;
    if (!agentId) return null;
    return {
      panelId: chatPanelId(agentId),
      component: "chat",
      title: "Chat",
      params: { agentId },
      renderer: "always",
    };
  }

  if (key === "file-viewer") {
    const p: Record<string, string> = {};
    if (params.path) p.path = params.path;
    return { panelId: "file-viewer", component: "file-viewer", title: "Files", params: p };
  }

  if (key === "agent-inspector") {
    const p: Record<string, string> = { controlKey: "agent-inspector" };
    if (params.agent) p.agentId = params.agent;
    if (params.inspectorTab) p.initialTab = params.inspectorTab;
    return { panelId: "agent-inspector", component: "control", title: "Agent Inspector", params: p };
  }

  if (key.startsWith("teams-")) {
    const teamsKey = key.slice("teams-".length);
    if (teamsKey in TEAMS_DETAIL_KEYS) {
      const detailId = params.detailId;
      if (!detailId) return null;
      return {
        panelId: `${key}:${detailId}`,
        component: "teams",
        title: TEAMS_DETAIL_KEYS[teamsKey],
        params: { teamsKey, detailId },
      };
    }
    if (teamsKey in TEAMS_LIST_KEYS) {
      const p: Record<string, string> = { teamsKey };
      // Durable filters ride along; one-shots are handled by the reconciler
      // at open time and never enter these params.
      if (teamsKey === "issues" && params.agent) p.agent = params.agent;
      return { panelId: key, component: "teams", title: TEAMS_LIST_KEYS[teamsKey], params: p };
    }
    return null;
  }

  if (key in CONTROL_KEYS) {
    const p: Record<string, string> = { controlKey: key };
    if (key === "agents" && params.agent) p.agentId = params.agent;
    return { panelId: `control:${key}`, component: "control", title: CONTROL_KEYS[key], params: p };
  }

  return null;
}

// Which URL params "belong" to a panel when it is the URL-named panel —
// the clear-on-close replaceState clears exactly this set so a refresh
// can't resurrect a deliberately closed panel with stale filter state.
export function durableParamsFor(panelId: string): string[] {
  if (panelId.startsWith("chat:")) return []; // chat is never URL-named
  if (panelId === "agent-inspector") return ["panel", "agent", "inspectorTab"];
  if (panelId === "control:agents") return ["panel", "agent"];
  if (panelId === "teams-issues") return ["panel", "agent"];
  if (/^teams-(issue|routine|project|goal)-detail:/.test(panelId)) return ["panel", "detailId"];
  return ["panel"];
}
```

- [ ] **Step 4: Run test + typecheck, commit**

```bash
pnpm vitest run src/components/workspace/__tests__/panelRegistry.test.ts --silent && pnpm exec tsc --noEmit
git add src/components/workspace/panelRegistry.ts src/components/workspace/__tests__/panelRegistry.test.ts
git commit -m "feat(workspace): panel registry — ids, aliases, param classes"
```

---

### Task 4: Layout storage (blob + sanitizer)

**Files:**
- Create: `src/components/workspace/layoutStorage.ts`
- Test: `src/components/workspace/__tests__/layoutStorage.test.ts`

**Interfaces:**
- Consumes: `PANEL_COMPONENT_KEYS`, `ONE_SHOT_PARAMS` from `./panelRegistry`; `SerializedDockview` type from `dockview-react`.
- Produces:

```ts
export function layoutStorageKey(scopeId: string): string;   // isol8.workspace.layout.v1:<scope>
export function readLayout(scopeId: string): SerializedDockview | null;  // validated + sanitized, null on any failure
export function writeLayout(scopeId: string, layout: SerializedDockview): void;  // swallow-on-fail
export function clearLayout(scopeId: string): void;          // removeItem — NEVER localStorage.clear()
export function sanitizeLayout(raw: SerializedDockview): SerializedDockview; // exported for tests
```

- [ ] **Step 1: Write the failing test**

```ts
// src/components/workspace/__tests__/layoutStorage.test.ts
import { describe, it, expect, beforeEach, afterAll } from "vitest";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";
import type { SerializedDockview } from "dockview-react";
import {
  layoutStorageKey,
  readLayout,
  writeLayout,
  clearLayout,
  sanitizeLayout,
} from "../layoutStorage";

const restore = installStoragePolyfill();
afterAll(restore);
beforeEach(() => window.localStorage.clear());

function minimalLayout(panels: SerializedDockview["panels"]): SerializedDockview {
  return {
    grid: {
      root: { type: "branch", data: [] },
      height: 600,
      width: 800,
      orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
    },
    panels,
  };
}

describe("layoutStorage", () => {
  it("scopes the key per owner", () => {
    expect(layoutStorageKey("org_1")).toBe("isol8.workspace.layout.v1:org_1");
  });

  it("round-trips a valid layout", () => {
    const layout = minimalLayout({
      "teams-inbox": { id: "teams-inbox", contentComponent: "teams", params: { teamsKey: "inbox" } },
    });
    writeLayout("u1", layout);
    expect(readLayout("u1")).toEqual(layout);
  });

  it("returns null on version mismatch or garbage", () => {
    window.localStorage.setItem(layoutStorageKey("u1"), JSON.stringify({ version: 2, layout: {} }));
    expect(readLayout("u1")).toBeNull();
    window.localStorage.setItem(layoutStorageKey("u1"), "not-json{");
    expect(readLayout("u1")).toBeNull();
  });

  it("sanitizer remaps unknown contentComponent to 'missing' (fromJSON throws otherwise)", () => {
    const dirty = minimalLayout({
      ghost: { id: "ghost", contentComponent: "deleted-panel-type", params: {} },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels.ghost.contentComponent).toBe("missing");
  });

  it("sanitizer strips floating/popout/edge groups and one-shot params", () => {
    const dirty = {
      ...minimalLayout({
        "teams-issues": {
          id: "teams-issues",
          contentComponent: "teams",
          params: { teamsKey: "issues", new: "task", agent: "a-1" },
        },
      }),
      floatingGroups: [{}],
      popoutGroups: [{}],
      edgeGroups: {},
    } as unknown as SerializedDockview;
    const clean = sanitizeLayout(dirty);
    expect(clean.floatingGroups).toBeUndefined();
    expect(clean.popoutGroups).toBeUndefined();
    expect((clean as Record<string, unknown>).edgeGroups).toBeUndefined();
    expect(clean.panels["teams-issues"].params).toEqual({ teamsKey: "issues", agent: "a-1" });
  });

  it("clearLayout removes only its own key", () => {
    writeLayout("u1", minimalLayout({}));
    window.localStorage.setItem("unrelated", "x");
    clearLayout("u1");
    expect(readLayout("u1")).toBeNull();
    expect(window.localStorage.getItem("unrelated")).toBe("x");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/components/workspace/__tests__/layoutStorage.test.ts --silent`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```ts
// src/components/workspace/layoutStorage.ts
// Persisted dockview layout blob. Template: desktopPermissions.ts — version
// in the key AND the payload, strict validation, null-on-any-failure reads,
// swallow-on-failure writes (private browsing / quota → session just isn't
// persistent). NEVER call localStorage.clear() here: a key===null storage
// event is treated as a permissions event by the gateway listeners.
import type { SerializedDockview } from "dockview-react";
import { PANEL_COMPONENT_KEYS, ONE_SHOT_PARAMS } from "./panelRegistry";

const KEY_PREFIX = "isol8.workspace.layout.v1";

interface StoredLayout {
  version: 1;
  savedAt: string;
  layout: SerializedDockview;
}

export function layoutStorageKey(scopeId: string): string {
  return `${KEY_PREFIX}:${scopeId}`;
}

// fromJSON THROWS (after clearing the dock) on an unregistered
// contentComponent — one stale key must not nuke the whole saved layout.
// Component names live only in the flat panels map (the grid tree references
// panel IDs), so remapping unknowns to the registered 'missing' placeholder
// is sufficient — no grid surgery needed. Also register 'unknown' in the
// components map: the deserializer defaults a missing contentComponent to
// the literal string 'unknown'.
export function sanitizeLayout(raw: SerializedDockview): SerializedDockview {
  const panels: SerializedDockview["panels"] = {};
  for (const [id, state] of Object.entries(raw.panels ?? {})) {
    const component =
      state.contentComponent && PANEL_COMPONENT_KEYS.has(state.contentComponent)
        ? state.contentComponent
        : "missing";
    let params = state.params;
    if (params) {
      params = { ...params };
      for (const oneShot of ONE_SHOT_PARAMS) delete params[oneShot];
    }
    panels[id] = { ...state, contentComponent: component, params };
  }
  const clean: SerializedDockview = { grid: raw.grid, panels };
  if (raw.activeGroup !== undefined) clean.activeGroup = raw.activeGroup;
  // floatingGroups / popoutGroups / edgeGroups deliberately dropped:
  // disableFloatingGroups only blocks user-initiated floats — persisted
  // floating groups would still restore (and popouts window.open a 404).
  return clean;
}

export function readLayout(scopeId: string): SerializedDockview | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(layoutStorageKey(scopeId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredLayout>;
    if (parsed.version !== 1 || !parsed.layout || typeof parsed.layout !== "object") return null;
    return sanitizeLayout(parsed.layout);
  } catch {
    return null;
  }
}

export function writeLayout(scopeId: string, layout: SerializedDockview): void {
  if (typeof window === "undefined") return;
  try {
    const stored: StoredLayout = {
      version: 1,
      savedAt: new Date().toISOString(),
      layout: sanitizeLayout(layout),
    };
    window.localStorage.setItem(layoutStorageKey(scopeId), JSON.stringify(stored));
  } catch {
    // quota / privacy mode — non-persistent session, never crash
  }
}

export function clearLayout(scopeId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(layoutStorageKey(scopeId));
  } catch {
    // best effort
  }
}
```

- [ ] **Step 4: Run test + typecheck, commit**

```bash
pnpm vitest run src/components/workspace/__tests__/layoutStorage.test.ts --silent && pnpm exec tsc --noEmit
git add src/components/workspace/layoutStorage.ts src/components/workspace/__tests__/layoutStorage.test.ts
git commit -m "feat(workspace): versioned layout blob storage with fromJSON-safe sanitizer"
```

---

### Task 5: URL command channel

**Files:**
- Create: `src/components/workspace/urlChannel.ts`
- Test: `src/components/workspace/__tests__/urlChannel.test.ts`

**Interfaces:**
- Consumes: `describePanel`, `resolvePanelAlias`, `ONE_SHOT_PARAMS`, `durableParamsFor`, `chatPanelId` from `./panelRegistry`.
- Produces (consumed by WorkspaceShell's reconciler and the controller's URL writer):

```ts
export interface WorkspaceCommand {
  descriptor: PanelDescriptor;
  oneShots: Record<string, string>;   // e.g. { new: "task" } — open-time params, then cleared
}
export function parseCommand(search: string): WorkspaceCommand | null;  // null = no command (bare /chat etc.)
export function commandSignature(search: string): string;               // stable string for the lastApplied guard
export function stripOneShots(search: string): string | null;           // new search string, or null if nothing to strip
export function buildPanelUrl(panelKey: string, params: Record<string, string>): string; // outbound push URLs
export function clearPanelFromUrl(search: string, panelId: string, urlPanelKey: string): string | null;
```

- [ ] **Step 1: Write the failing test**

```ts
// src/components/workspace/__tests__/urlChannel.test.ts
import { describe, it, expect } from "vitest";
import {
  parseCommand,
  commandSignature,
  stripOneShots,
  buildPanelUrl,
  clearPanelFromUrl,
} from "../urlChannel";

describe("parseCommand", () => {
  it("parses a teams list command", () => {
    const c = parseCommand("?panel=teams-issues&agent=a-1");
    expect(c?.descriptor.panelId).toBe("teams-issues");
    expect(c?.descriptor.params).toEqual({ teamsKey: "issues", agent: "a-1" });
    expect(c?.oneShots).toEqual({});
  });

  it("separates one-shots from durable params", () => {
    const c = parseCommand("?panel=teams-issues&new=task");
    expect(c?.descriptor.params).toEqual({ teamsKey: "issues" });
    expect(c?.oneShots).toEqual({ new: "task" });
  });

  it("resolves retired aliases", () => {
    expect(parseCommand("?panel=teams-approvals")?.descriptor.panelId).toBe("teams-inbox");
    expect(parseCommand("?panel=credits")?.descriptor.panelId).toBe("control:llm");
  });

  it("defines /chat?agent= as open-or-focus that agent's chat (deploy-success link)", () => {
    const c = parseCommand("?agent=agent-uuid-1");
    expect(c?.descriptor.panelId).toBe("chat:agent-uuid-1");
    expect(c?.descriptor.component).toBe("chat");
  });

  it("returns null for bare /chat, billing returns, and unknown panels", () => {
    expect(parseCommand("")).toBeNull();
    expect(parseCommand("?checkout=success")).toBeNull();
    expect(parseCommand("?panel=definitely-not-a-panel")).toBeNull();
    expect(parseCommand("?panel=teams-issue-detail")).toBeNull(); // detail without id
  });
});

describe("stripOneShots", () => {
  it("removes exactly the one-shot params", () => {
    expect(stripOneShots("?panel=teams-inbox&newIssue=onboarding&detailId=x")).toBe(
      "panel=teams-inbox&detailId=x",
    );
  });
  it("returns null when nothing to strip", () => {
    expect(stripOneShots("?panel=teams-inbox")).toBeNull();
  });
});

describe("buildPanelUrl", () => {
  it("builds canonical outbound URLs", () => {
    expect(buildPanelUrl("teams-issue-detail", { detailId: "ADM-9" })).toBe(
      "/chat?panel=teams-issue-detail&detailId=ADM-9",
    );
    expect(buildPanelUrl("agent-inspector", { agent: "a-1", inspectorTab: "channels" })).toBe(
      "/chat?panel=agent-inspector&agent=a-1&inspectorTab=channels",
    );
  });
});

describe("clearPanelFromUrl", () => {
  it("clears the full durable set when the closed panel is URL-named", () => {
    expect(
      clearPanelFromUrl("?panel=agent-inspector&agent=a-1&inspectorTab=channels", "agent-inspector", "agent-inspector"),
    ).toBe("");
    expect(
      clearPanelFromUrl("?panel=teams-issue-detail&detailId=ADM-9", "teams-issue-detail:ADM-9", "teams-issue-detail"),
    ).toBe("");
  });
  it("returns null when the closed panel is not the URL-named one", () => {
    expect(clearPanelFromUrl("?panel=teams-inbox", "teams-issues", "teams-inbox")).toBeNull();
    expect(
      clearPanelFromUrl("?panel=teams-issue-detail&detailId=OTHER", "teams-issue-detail:ADM-9", "teams-issue-detail"),
    ).toBeNull();
  });
});

describe("commandSignature", () => {
  it("ignores non-command params so billing strips don't retrigger the reconciler", () => {
    expect(commandSignature("?panel=teams-inbox&checkout=success")).toBe(
      commandSignature("?panel=teams-inbox"),
    );
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/components/workspace/__tests__/urlChannel.test.ts --silent`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```ts
// src/components/workspace/urlChannel.ts
// The URL is a ONE-WAY retained command channel (spec §6): inbound ?panel=…
// is a navigation intent the shell executes as open-or-focus; outbound, only
// explicit navigations write it. Focus and layout changes never touch it.
import {
  describePanel,
  durableParamsFor,
  ONE_SHOT_PARAMS,
  NON_COMMAND_PARAMS,
  type PanelDescriptor,
} from "./panelRegistry";

export interface WorkspaceCommand {
  descriptor: PanelDescriptor;
  oneShots: Record<string, string>;
}

function toParams(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
}

export function parseCommand(search: string): WorkspaceCommand | null {
  const params = toParams(search);
  const all: Record<string, string> = {};
  params.forEach((v, k) => {
    all[k] = v;
  });

  const oneShots: Record<string, string> = {};
  for (const k of ONE_SHOT_PARAMS) {
    if (all[k] !== undefined) {
      oneShots[k] = all[k];
      delete all[k];
    }
  }
  for (const k of NON_COMMAND_PARAMS) delete all[k];

  const panelKey = all.panel;
  delete all.panel;

  if (!panelKey) {
    // Bare ?agent= (marketplace deploy-success) = open that agent's chat.
    if (all.agent) {
      const descriptor = describePanel("chat", { agent: all.agent });
      return descriptor ? { descriptor, oneShots } : null;
    }
    return null;
  }

  const descriptor = describePanel(panelKey, all);
  return descriptor ? { descriptor, oneShots } : null;
}

// Stable identity of "what command does this URL carry" — the reconciler's
// lastApplied guard compares signatures, so billing-return strips and other
// non-command churn never re-execute a command.
export function commandSignature(search: string): string {
  const params = toParams(search);
  const kept: string[] = [];
  const skip = new Set<string>(NON_COMMAND_PARAMS);
  [...params.keys()].sort().forEach((k) => {
    if (!skip.has(k)) kept.push(`${k}=${params.get(k)}`);
  });
  return kept.join("&");
}

export function stripOneShots(search: string): string | null {
  const params = toParams(search);
  let changed = false;
  for (const k of ONE_SHOT_PARAMS) {
    if (params.has(k)) {
      params.delete(k);
      changed = true;
    }
  }
  return changed ? params.toString() : null;
}

export function buildPanelUrl(panelKey: string, params: Record<string, string>): string {
  const sp = new URLSearchParams();
  sp.set("panel", panelKey);
  for (const [k, v] of Object.entries(params)) sp.set(k, v);
  return `/chat?${sp.toString()}`;
}

// Closing the URL-named panel canonicalizes the URL in place (replace, never
// push) so a refresh can't resurrect the panel the user just closed.
// urlPanelKey is the raw ?panel value (alias-resolved by the caller).
export function clearPanelFromUrl(
  search: string,
  closedPanelId: string,
  urlPanelKey: string,
): string | null {
  const command = parseCommand(search);
  if (!command || command.descriptor.panelId !== closedPanelId) return null;
  const params = toParams(search);
  for (const k of durableParamsFor(closedPanelId)) params.delete(k);
  for (const k of ONE_SHOT_PARAMS) params.delete(k);
  void urlPanelKey;
  return params.toString();
}
```

- [ ] **Step 4: Run test + typecheck, commit**

```bash
pnpm vitest run src/components/workspace/__tests__/urlChannel.test.ts --silent && pnpm exec tsc --noEmit
git add src/components/workspace/urlChannel.ts src/components/workspace/__tests__/urlChannel.test.ts
git commit -m "feat(workspace): URL command channel — parse, strip, build, clear-on-close"
```

---

### Task 6: Workspace controller (module store over DockviewApi)

**Files:**
- Create: `src/components/workspace/workspaceController.ts`
- Test: `src/components/workspace/__tests__/workspaceController.test.ts`

**Interfaces:**
- Consumes: `describePanel`, `chatPanelId`, `DEFAULT_AGENT_SENTINEL` from `./panelRegistry`; `buildPanelUrl` from `./urlChannel`; `pushSearchParams` from `@/lib/shellNav`; `DockviewApi`, `AddPanelOptions` types from `dockview-react`.
- Produces (the ONLY sanctioned way anything opens/focuses panels — spec §6 rule "all programmatic opens funnel through one shell API"):

```ts
export function attachDockviewApi(api: DockviewApi, resolveDefaultAgentId: () => string | undefined): () => void; // returns detach
export function openPanel(panelKey: string, params?: Record<string, string>, opts?: { pushUrl?: boolean; oneShots?: Record<string, string>; focus?: boolean }): void;
export function openChatPanel(agentId: string): void;   // sentinel/concrete unification
export function closeSubjectDeleted(selfPanelId: string, listPanelKey: string): void;
export function useWorkspaceState(): WorkspaceSnapshot;  // { ready, activePanelId, focusedChatAgentId, visibleChatAgentIds, teamsPanelVisible, openPanelIds }
export function isShellOperationInProgress(): boolean;   // clear-on-close suppression during compound ops
export function resetWorkspaceLayout(build: (api: DockviewApi) => void): void;
export function _resetControllerForTests(): void;
```

- [ ] **Step 1: Write the failing test** (fake DockviewApi — the controller only uses `getPanel`/`addPanel`/`removePanel`/`clear` + events, so a hand-rolled fake suffices; follow `useActiveSession`'s module-store test patterns)

```ts
// src/components/workspace/__tests__/workspaceController.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { DockviewApi, IDockviewPanel } from "dockview-react";
import {
  attachDockviewApi,
  openPanel,
  openChatPanel,
  closeSubjectDeleted,
  isShellOperationInProgress,
  _resetControllerForTests,
  _snapshotForTests,
} from "../workspaceController";

vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";

type Handler<T> = (e: T) => void;

function makeFakeApi() {
  const panels = new Map<string, FakePanel>();
  const activeChange: Handler<{ panel: FakePanel | undefined; origin: "user" | "api" }>[] = [];
  const removed: Handler<FakePanel>[] = [];
  class FakePanel {
    api = {
      setActive: vi.fn(() => {
        fire(this);
      }),
      updateParameters: vi.fn(),
      close: vi.fn(() => {
        panels.delete(this.id);
        removed.forEach((h) => h(this));
      }),
    };
    constructor(
      public id: string,
      public params: Record<string, string>,
    ) {}
  }
  function fire(panel: FakePanel | undefined) {
    activeChange.forEach((h) => h({ panel, origin: "api" }));
  }
  const api = {
    getPanel: (id: string) => panels.get(id),
    addPanel: vi.fn((opts: { id: string; params?: Record<string, string> }) => {
      if (panels.has(opts.id)) throw new Error(`dockview: panel with id ${opts.id} already exists`);
      const p = new FakePanel(opts.id, opts.params ?? {});
      panels.set(opts.id, p);
      fire(p);
      return p;
    }),
    removePanel: vi.fn((p: FakePanel) => p.api.close()),
    clear: vi.fn(() => panels.clear()),
    onDidActivePanelChange: (h: Handler<{ panel: FakePanel | undefined; origin: "user" | "api" }>) => {
      activeChange.push(h);
      return { dispose: () => activeChange.splice(activeChange.indexOf(h), 1) };
    },
    onDidRemovePanel: (h: Handler<FakePanel>) => {
      removed.push(h);
      return { dispose: () => removed.splice(removed.indexOf(h), 1) };
    },
    onDidAddPanel: (h: Handler<FakePanel>) => ({ dispose: vi.fn() }),
    onDidLayoutChange: (h: Handler<void>) => ({ dispose: vi.fn() }),
    panels: [] as IDockviewPanel[],
  };
  Object.defineProperty(api, "panels", { get: () => [...panels.values()] });
  return api as unknown as DockviewApi & { addPanel: ReturnType<typeof vi.fn> };
}

beforeEach(() => {
  _resetControllerForTests();
  vi.clearAllMocks();
});

describe("workspaceController", () => {
  it("openPanel adds when missing, focuses when present, never duplicates", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openPanel("teams-inbox");
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    openPanel("teams-inbox");
    expect(api.addPanel).toHaveBeenCalledTimes(1); // focused, not re-added
  });

  it("openPanel with pushUrl writes the canonical URL once", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("teams-issue-detail", { detailId: "ADM-1" }, { pushUrl: true });
    expect(pushSearchParams).toHaveBeenCalledTimes(1);
    const arg = (pushSearchParams as ReturnType<typeof vi.fn>).mock.calls[0][0] as URLSearchParams;
    expect(arg.get("panel")).toBe("teams-issue-detail");
    expect(arg.get("detailId")).toBe("ADM-1");
  });

  it("openChatPanel unifies sentinel and concrete ids", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openPanel("chat", { agent: "__default__" }); // sentinel panel exists (default layout)
    expect(api.getPanel("chat:__default__")).toBeDefined();
    openChatPanel("friday-id"); // concrete id that RESOLVES to the sentinel → focus it, no new panel
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    openChatPanel("other-agent"); // different agent → new panel
    expect(api.addPanel).toHaveBeenCalledTimes(2);
    expect(api.getPanel("chat:other-agent")).toBeDefined();
  });

  it("chat panels are added with renderer always", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openChatPanel("a-1");
    expect(api.addPanel).toHaveBeenCalledWith(expect.objectContaining({ renderer: "always" }));
  });

  it("closeSubjectDeleted replaces URL, closes self with suppression up, opens the list", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("teams-issue-detail", { detailId: "ADM-1" });
    let suppressedDuringClose = false;
    api.onDidRemovePanel(() => {
      suppressedDuringClose = isShellOperationInProgress();
    });
    closeSubjectDeleted("teams-issue-detail:ADM-1", "teams-issues");
    expect(replaceSearchParams).toHaveBeenCalled(); // URL → list, replace not push
    expect(suppressedDuringClose).toBe(true);       // clear-on-close listener must skip
    expect(api.getPanel("teams-issue-detail:ADM-1")).toBeUndefined();
    expect(api.getPanel("teams-issues")).toBeDefined();
    expect(isShellOperationInProgress()).toBe(false);
  });

  it("tracks focused chat agent in the snapshot", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openChatPanel("a-1");
    expect(_snapshotForTests().focusedChatAgentId).toBe("a-1");
    openPanel("teams-inbox");
    expect(_snapshotForTests().focusedChatAgentId).toBeNull();
    expect(_snapshotForTests().activePanelId).toBe("teams-inbox");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/components/workspace/__tests__/workspaceController.test.ts --silent`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```ts
// src/components/workspace/workspaceController.ts
// Module-level singleton owning the DockviewApi. The repo's store pattern
// (useActiveSession): module state + Set of listeners + useSyncExternalStore.
// EVERY programmatic open/focus goes through openPanel/openChatPanel — a
// future toast or agent event that bypasses this API silently loses URL
// integration, so nothing else may call api.addPanel.
"use client";

import { useSyncExternalStore, useCallback } from "react";
import type { DockviewApi, IDockviewPanel, IDisposable } from "dockview-react";
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import {
  describePanel,
  chatPanelId,
  DEFAULT_AGENT_SENTINEL,
  type PanelDescriptor,
} from "./panelRegistry";
import { buildPanelUrl } from "./urlChannel";

export interface WorkspaceSnapshot {
  ready: boolean;
  activePanelId: string | null;
  focusedChatAgentId: string | null; // sentinel RESOLVED when possible
  teamsPanelVisible: boolean;
  openPanelIds: string[];
}

let _api: DockviewApi | null = null;
let _resolveDefaultAgentId: () => string | undefined = () => undefined;
let _shellOpDepth = 0;
let _snapshot: WorkspaceSnapshot = emptySnapshot();
const _listeners = new Set<() => void>();

function emptySnapshot(): WorkspaceSnapshot {
  return {
    ready: false,
    activePanelId: null,
    focusedChatAgentId: null,
    teamsPanelVisible: false,
    openPanelIds: [],
  };
}

function notify(): void {
  _listeners.forEach((l) => l());
}

function agentIdOfChatPanel(panel: IDockviewPanel | undefined): string | null {
  if (!panel || !panel.id.startsWith("chat:")) return null;
  const raw = panel.id.slice("chat:".length);
  if (raw === DEFAULT_AGENT_SENTINEL) return _resolveDefaultAgentId() ?? null;
  return raw;
}

function recomputeSnapshot(): void {
  if (!_api) {
    _snapshot = emptySnapshot();
    notify();
    return;
  }
  const active = _api.activePanel;
  const openPanelIds = _api.panels.map((p) => p.id);
  _snapshot = {
    ready: true,
    activePanelId: active?.id ?? null,
    focusedChatAgentId: agentIdOfChatPanel(active),
    teamsPanelVisible: openPanelIds.some((id) => id.startsWith("teams-")),
    openPanelIds,
  };
  notify();
}

export function attachDockviewApi(
  api: DockviewApi,
  resolveDefaultAgentId: () => string | undefined,
): () => void {
  _api = api;
  _resolveDefaultAgentId = resolveDefaultAgentId;
  const disposables: IDisposable[] = [
    api.onDidActivePanelChange(() => recomputeSnapshot()),
    api.onDidAddPanel(() => recomputeSnapshot()),
    api.onDidRemovePanel(() => recomputeSnapshot()),
  ];
  recomputeSnapshot();
  return () => {
    disposables.forEach((d) => d.dispose());
    _api = null;
    _resolveDefaultAgentId = () => undefined;
    recomputeSnapshot();
  };
}

function addOrFocus(descriptor: PanelDescriptor, extraParams?: Record<string, string>): void {
  if (!_api) return;
  const existing = _api.getPanel(descriptor.panelId);
  if (existing) {
    if (extraParams && Object.keys(extraParams).length > 0) {
      // One-shot re-delivery to an already-open panel (e.g. a second
      // ?new=task command): panels watch these via onDidParametersChange.
      existing.api.updateParameters(extraParams);
    }
    existing.api.setActive();
    return;
  }
  _api.addPanel({
    id: descriptor.panelId,
    component: descriptor.component,
    title: descriptor.title,
    params: { ...descriptor.params, ...extraParams },
    ...(descriptor.renderer ? { renderer: descriptor.renderer } : {}),
  });
}

export function openPanel(
  panelKey: string,
  params: Record<string, string> = {},
  opts: { pushUrl?: boolean; oneShots?: Record<string, string>; focus?: boolean } = {},
): void {
  const descriptor = describePanel(panelKey, params);
  if (!descriptor) return;
  if (descriptor.component === "chat") {
    // Chat identity needs sentinel unification — delegate.
    openChatPanel(descriptor.params.agentId);
    return;
  }
  addOrFocus(descriptor, opts.oneShots);
  if (opts.pushUrl) {
    pushSearchParams(new URLSearchParams(buildPanelUrl(panelKey, params).split("?")[1]));
  }
}

// One chat panel per agent. A sentinel panel (`chat:__default__`, from the
// Mission Control default layout) durably means "the CEO/default agent" —
// opening the concrete agent it currently resolves to must focus the
// sentinel panel, not open a duplicate.
export function openChatPanel(agentId: string): void {
  if (!_api) return;
  const concrete = _api.getPanel(chatPanelId(agentId));
  if (concrete) {
    concrete.api.setActive();
    return;
  }
  if (agentId !== DEFAULT_AGENT_SENTINEL) {
    const sentinel = _api.getPanel(chatPanelId(DEFAULT_AGENT_SENTINEL));
    if (sentinel && _resolveDefaultAgentId() === agentId) {
      sentinel.api.setActive();
      return;
    }
  } else {
    const resolved = _resolveDefaultAgentId();
    if (resolved) {
      const concreteDefault = _api.getPanel(chatPanelId(resolved));
      if (concreteDefault) {
        concreteDefault.api.setActive();
        return;
      }
    }
  }
  const descriptor = describePanel("chat", { agent: agentId });
  if (descriptor) addOrFocus(descriptor);
}

// Sanctioned exception to "close never touches the URL" (spec §6 rule 8):
// same-tab delete of the URL-named detail. Order pinned: (a) replace URL to
// the list, (b) close self with the clear-on-close listener suppressed,
// (c) open-or-focus the list panel.
export function closeSubjectDeleted(selfPanelId: string, listPanelKey: string): void {
  if (!_api) return;
  const listUrl = buildPanelUrl(listPanelKey, {});
  replaceSearchParams(new URLSearchParams(listUrl.split("?")[1]));
  _shellOpDepth += 1;
  try {
    const self = _api.getPanel(selfPanelId);
    if (self) _api.removePanel(self);
    openPanel(listPanelKey);
  } finally {
    _shellOpDepth -= 1;
  }
}

export function isShellOperationInProgress(): boolean {
  return _shellOpDepth > 0;
}

// Reset = clear + rebuild the code-built default. reuseExistingPanels keeps
// matching panel instances alive (a live chat panel survives a reset).
export function resetWorkspaceLayout(build: (api: DockviewApi) => void): void {
  if (!_api) return;
  _shellOpDepth += 1;
  try {
    _api.clear();
    build(_api);
  } finally {
    _shellOpDepth -= 1;
  }
}

function subscribe(l: () => void): () => void {
  _listeners.add(l);
  return () => _listeners.delete(l);
}

export function useWorkspaceState(): WorkspaceSnapshot {
  return useSyncExternalStore(
    useCallback(subscribe, []),
    () => _snapshot,
    () => _snapshot,
  );
}

export function _snapshotForTests(): WorkspaceSnapshot {
  return _snapshot;
}

export function _resetControllerForTests(): void {
  _api = null;
  _resolveDefaultAgentId = () => undefined;
  _shellOpDepth = 0;
  _snapshot = emptySnapshot();
  _listeners.clear();
}
```

- [ ] **Step 4: Run test + typecheck, commit**

```bash
pnpm vitest run src/components/workspace/__tests__/workspaceController.test.ts --silent && pnpm exec tsc --noEmit
git add src/components/workspace/workspaceController.ts src/components/workspace/__tests__/workspaceController.test.ts
git commit -m "feat(workspace): controller store — open/focus API over DockviewApi"
```

---

### Task 7: Mission Control default layout

**Files:**
- Create: `src/components/workspace/buildDefaultLayout.ts`
- Test: `src/components/workspace/__tests__/buildDefaultLayout.test.ts`

**Interfaces:**
- Consumes: `chatPanelId`, `DEFAULT_AGENT_SENTINEL` from `./panelRegistry`; `DockviewApi` type.
- Produces: `buildMissionControlLayout(api: DockviewApi): void` — sentinel Friday chat left (~55%), Tasks board top-right, Inbox below it. A code function so the default evolves with releases (never a stored blob).

- [ ] **Step 1: Write the failing test**

```ts
// src/components/workspace/__tests__/buildDefaultLayout.test.ts
import { describe, it, expect, vi } from "vitest";
import type { DockviewApi } from "dockview-react";
import { buildMissionControlLayout } from "../buildDefaultLayout";

describe("buildMissionControlLayout", () => {
  it("opens sentinel chat, tasks right, inbox below tasks — chat active last", () => {
    const calls: Array<Record<string, unknown>> = [];
    const api = {
      addPanel: vi.fn((opts: Record<string, unknown>) => {
        calls.push(opts);
        return { id: opts.id, api: { setActive: vi.fn() } };
      }),
      getPanel: vi.fn((id: string) => {
        const found = calls.find((c) => c.id === id);
        return found ? { id, api: { setActive: vi.fn() } } : undefined;
      }),
    } as unknown as DockviewApi;

    buildMissionControlLayout(api);

    expect(calls.map((c) => c.id)).toEqual(["chat:__default__", "teams-issues", "teams-inbox"]);
    expect(calls[0]).toMatchObject({ component: "chat", renderer: "always" });
    expect(calls[1]).toMatchObject({
      component: "teams",
      position: { referencePanel: "chat:__default__", direction: "right" },
    });
    expect(calls[2]).toMatchObject({
      component: "teams",
      position: { referencePanel: "teams-issues", direction: "below" },
    });
    // Chat ends focused (spec: default layout focuses the conversation)
    expect(calls[0].id).toBe("chat:__default__");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/components/workspace/__tests__/buildDefaultLayout.test.ts --silent`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```ts
// src/components/workspace/buildDefaultLayout.ts
// Mission Control: sentinel CEO chat on the left, Tasks board and Inbox
// stacked on the right. Built by code (never persisted as a preset blob) so
// the default evolves with releases; Reset re-applies it.
import type { DockviewApi } from "dockview-react";
import { chatPanelId, DEFAULT_AGENT_SENTINEL } from "./panelRegistry";

export function buildMissionControlLayout(api: DockviewApi): void {
  const chatId = chatPanelId(DEFAULT_AGENT_SENTINEL);
  api.addPanel({
    id: chatId,
    component: "chat",
    title: "Chat",
    params: { agentId: DEFAULT_AGENT_SENTINEL },
    renderer: "always",
  });
  api.addPanel({
    id: "teams-issues",
    component: "teams",
    title: "Tasks",
    params: { teamsKey: "issues" },
    position: { referencePanel: chatId, direction: "right" },
    // Chat keeps ~55% — dockview splits 50/50 by default; initialWidth on
    // the right group nudges it. 45% of a 1440 dock ≈ 648.
    initialWidth: 648,
  });
  api.addPanel({
    id: "teams-inbox",
    component: "teams",
    title: "Inbox",
    params: { teamsKey: "inbox" },
    position: { referencePanel: "teams-issues", direction: "below" },
  });
  api.getPanel(chatId)?.api.setActive();
}
```

- [ ] **Step 4: Run test + typecheck, commit**

```bash
pnpm vitest run src/components/workspace/__tests__/buildDefaultLayout.test.ts --silent && pnpm exec tsc --noEmit
git add src/components/workspace/buildDefaultLayout.ts src/components/workspace/__tests__/buildDefaultLayout.test.ts
git commit -m "feat(workspace): Mission Control default layout builder"
```

---

### Task 8: Panel param prop-threading (both shells feed panels; panels stop reading the URL)

Panels are shared with the mobile ChatLayout path, so they can't just switch to dockview params — they take **props**, and each shell supplies them (mobile: derived from the URL at page level, exactly today's values; desktop: from dockview panel params). One-shot consumption flows back through callbacks so each shell strips its own channel.

**Files:**
- Modify: `src/components/teams/TeamsPanelRouter.tsx` (accept + thread `panelParams`, delete the mount-time alias URL rewrite at :88-94 — aliases now resolve in the registry)
- Modify: `src/components/control/ControlPanelRouter.tsx` (same; delete the credits remap effect at :42-44)
- Modify: `src/components/teams/panels/IssuesPanel.tsx` (props replace `?agent`/`?new` reads at :36/:61; strips at :63-70/:104-108 become callbacks)
- Modify: `src/components/teams/inbox/InboxPage.tsx` (props replace `?newIssue` at :156-177; **delete the `?tab` URL sync entirely** at :118-132 — keep the `loadLastInboxTab`/`saveLastInboxTab` localStorage persistence, which already survives; do NOT touch `inbox/hooks/*`)
- Modify: `src/components/control/panels/LLMPanel.tsx` (prop replaces `?action` read at :263-265; delete the raw `window.history.replaceState` strip at :267-285)
- Modify: `src/components/control/panels/AgentInspectorPanel.tsx` (props replace `?agent`/`?inspectorTab` reads at :50-51/:86-92)
- Modify: `src/components/control/panels/AgentsPanel.tsx` (prop replaces `?agent` read at :37-39)
- Modify: `src/components/chat/InlineTeamsPanel.tsx` (thread `panelParams` + one-shot callbacks; extend `InlineTeamsNavValue`)
- Modify: `src/components/chat/InlineTeamsNav.tsx` (add `leaveToList` to the context value)
- Modify: `src/components/teams/issues/IssueDetailPage.tsx:183-188`, `src/components/teams/goals/GoalDetailPage.tsx:87-90` (delete self-nav via `leaveToList`)
- Modify: `src/app/chat/page.tsx` (mobile path derives `panelParams` from searchParams and supplies strip callbacks — preserving today's behavior byte-for-byte)
- Tests: `src/components/teams/panels/__tests__/` + `src/components/control/panels/__tests__/` — update existing suites' harnesses where they mocked `useSearchParams`; add prop-driven cases.

**Interfaces (produced — the dock panels in Task 9 consume these exact props):**

```ts
// TeamsPanelRouter
interface TeamsPanelRouterProps {
  panel: string;
  detailId?: string | null;
  panelParams?: Record<string, string>;          // { agent?, newIssue?, new? }
  onConsumeOneShot?: (key: "new" | "newIssue") => void;
  onUpdateDurableParam?: (key: "agent", value: string | null) => void;
}
// ControlPanelRouter
interface ControlPanelRouterProps {
  panel: string;
  onPanelChange?: (panel: string) => void;
  panelParams?: Record<string, string>;          // { agentId?, initialTab?, action? }
  onConsumeOneShot?: (key: "action") => void;
}
// InlineTeamsNavValue gains:
interface InlineTeamsNavValue {
  openDetail: (panelKey: string, id: string) => void;
  leaveToList: (listPanelKey: string) => void;   // detail-delete escape; NEW
}
```

Per-panel prop shapes (threaded by the routers from `panelParams`):
- `IssuesPanel`: `{ agentFilter?: string | null; newTask?: boolean; onNewTaskConsumed?: () => void; onAgentFilterCleared?: () => void }`. `closeNewTask` calls `onNewTaskConsumed` instead of the URL strip; `clearAgentFilter` calls `onAgentFilterCleared`. Keep re-delivery semantics: derive `newTaskOpen` from the prop with the render-time prev-compare pattern InboxPage uses today (a second `new=task` command to a mounted panel must re-open the dialog).
- `InboxPage`: `{ newIssue?: "1" | "onboarding" | null; onNewIssueConsumed?: () => void }` — keep its existing prev-param compare (lines 163-170) but driven by the prop; strip effect (:172-177) becomes `onNewIssueConsumed`.
- `LLMPanel`: `{ initialAction?: "change-provider" | null; onActionConsumed?: () => void }` — ALSO fix the re-delivery gap: watch the prop with a prev-compare (today's useState-initializer ignores a second command).
- `AgentInspectorPanel`: `{ agentId?: string | null; initialTab?: string | null }` — agentId prop replaces the live `?agent` read; initialTab stays read-once-per-value (prev-compare so a new command with a different tab re-applies).
- `AgentsPanel`: `{ initialAgentId?: string | null }`.

**Mobile page wiring (page.tsx)** — derive once, thread down (preserves today's behavior exactly; the params and strips still live in the URL on mobile):

```tsx
// page.tsx (mobile branch), replacing the current InlineTeamsPanel/ControlPanelRouter mounts:
const agentParam = searchParams.get("agent");
const oneShots = {
  new: searchParams.get("new") ?? undefined,
  newIssue: searchParams.get("newIssue") ?? undefined,
  action: searchParams.get("action") ?? undefined,
  inspectorTab: searchParams.get("inspectorTab") ?? undefined,
};
const stripParam = useCallback(
  (key: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (params.has(key)) {
      params.delete(key);
      replaceSearchParams(params);
    }
  },
  [searchParams],
);
// InlineTeamsPanel gets: panelParams={{ ...(agentParam ? { agent: agentParam } : {}), ...(oneShots.new ? { new: oneShots.new } : {}), ...(oneShots.newIssue ? { newIssue: oneShots.newIssue } : {}) }}
//   onConsumeOneShot={stripParam} onUpdateDurableParam={(k, v) => { const p = new URLSearchParams(searchParams.toString()); v === null ? p.delete(k) : p.set(k, v); replaceSearchParams(p); }}
// ControlPanelRouter gets: panelParams={{ ...(agentParam ? { agentId: agentParam } : {}), ...(oneShots.inspectorTab ? { initialTab: oneShots.inspectorTab } : {}), ...(oneShots.action ? { action: oneShots.action } : {}) }}
//   onConsumeOneShot={stripParam}
```

`InlineTeamsNavValue.leaveToList` mobile impl (in page.tsx, replacing the detail pages' self-nav):

```ts
const leaveToList = useCallback(
  (listPanelKey: string) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("panel", listPanelKey);
    params.delete("detailId");
    params.delete("agent");
    replaceSearchParams(params); // replace, never push — Back must not resurrect a deleted row
  },
  [searchParams],
);
```

`IssueDetailPage.tsx:183-188` / `GoalDetailPage.tsx:87-90` replace their `window.location.search` rewrites with `useInlineTeamsNav()?.leaveToList("teams-issues" | "teams-goals")` — keep a fallback to the old rewrite ONLY if the nav context is null (standalone/test renders).

- [ ] **Step 1: Write failing prop-driven tests for IssuesPanel + LLMPanel re-delivery** (extend existing suites; follow their harnesses)

```tsx
// Additions to the existing IssuesPanel suite (check its actual path under
// src/components/teams/panels/__tests__/ or src/__tests__/ and extend there):
it("opens the create dialog from the newTask prop and reports consumption on close", async () => {
  const onNewTaskConsumed = vi.fn();
  render(<IssuesPanel newTask onNewTaskConsumed={onNewTaskConsumed} />);
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(onNewTaskConsumed).toHaveBeenCalledTimes(1);
});

it("re-opens the dialog when newTask goes false→true on a mounted panel", async () => {
  const { rerender } = render(<IssuesPanel newTask={false} />);
  rerender(<IssuesPanel newTask />);
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify they fail** (props don't exist yet)

- [ ] **Step 3: Implement panel by panel, router by router** — mechanical conversion per the Interfaces block above. Rules: (a) NO panel keeps a `useSearchParams` import when done (grep proves it: `grep -l useSearchParams src/components/teams/panels src/components/control/panels src/components/teams/inbox/InboxPage.tsx` returns nothing); (b) every deleted strip has a callback replacement wired in the mobile page; (c) InboxPage's `?tab` handling is DELETED not converted (localStorage already persists the tab; verify `loadLastInboxTab` still seeds initial state); (d) keep each panel's existing default behavior when the new props are absent (undefined props = today's no-param state) so standalone renders and older tests stay green.

- [ ] **Step 4: Update existing tests that mocked useSearchParams for these panels** — convert their harnesses to props. Run each touched suite single-file.

- [ ] **Step 5: Verify + commit**

```bash
pnpm vitest run src/components/teams/panels/__tests__ --silent 2>/dev/null || true  # run files individually if dir run is heavy
pnpm exec tsc --noEmit && pnpm run lint --quiet && pnpm run build
git add -A src/components/teams src/components/control src/components/chat/InlineTeamsPanel.tsx src/components/chat/InlineTeamsNav.tsx src/app/chat/page.tsx
git commit -m "refactor(panels): params via props from both shells; panels stop reading the URL"
```

---

### Task 9: Dock panel host components + ChatPanel

**Files:**
- Create: `src/components/workspace/panels/ChatPanel.tsx`
- Create: `src/components/workspace/panels/TeamsDockPanel.tsx`
- Create: `src/components/workspace/panels/ControlDockPanel.tsx`
- Create: `src/components/workspace/panels/FileViewerDockPanel.tsx`
- Create: `src/components/workspace/panels/MissingPanel.tsx`
- Create: `src/components/workspace/panelComponents.ts` (the module-scope components map)
- Modify: `src/components/chat/ChatInput.tsx:130-153` (focus-composer event gains optional agentId targeting)
- Tests: `src/components/workspace/__tests__/ChatPanel.test.tsx`, `src/components/workspace/__tests__/MissingPanel.test.tsx`

**Interfaces:**
- Consumes: `IDockviewPanelProps` from dockview-react; `AgentChatWindow { agentId: string | null; onOpenFile?: (path: string) => void }`; `useAgents()` (`data !== undefined` ⇔ `agents`+`defaultId` settled — expose `isSettled` below); `useTeamsApi().read`, `useAgentMutations` (pause/resume, revalidates `/teams/agents`); `openPanel`, `openChatPanel` from `../workspaceController`; routers from Task 8.
- Produces: `PANEL_COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>>` with keys exactly `chat | teams | control | file-viewer | missing | unknown` (unknown → MissingPanel; the deserializer defaults missing contentComponent to the literal `'unknown'`).

**ChatPanel contract (write exactly this):**

```tsx
// src/components/workspace/panels/ChatPanel.tsx
"use client";

import { useMemo, useState } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { SlidersHorizontal, ListTodo, Pause, Play, X } from "lucide-react";
import { AgentChatWindow } from "@/components/chat/AgentChatWindow";
import { useAgents, agentDisplayName } from "@/hooks/useAgents";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";
import { openPanel } from "../workspaceController";

interface ChatPanelParams {
  agentId: string;
}

export function ChatPanel(props: IDockviewPanelProps<ChatPanelParams>): React.ReactElement {
  const { agents, defaultId, isSettled } = useAgents();
  const raw = props.params.agentId;
  const resolvedId = raw === DEFAULT_AGENT_SENTINEL ? (defaultId ?? null) : raw;

  // Agent-gone gating (spec §4): ONLY once the roster has settled
  // (data !== undefined). isLoading is false during SWR error-retry cycles
  // and the `?? []` default makes membership checks lie during connect —
  // both would false-fire this state on every persisted panel at every load.
  const agentGone =
    isSettled && raw !== DEFAULT_AGENT_SENTINEL && !agents.some((a) => a.id === raw);

  // Per-agent action cluster (moved from ChatLayout's header — it has no
  // single referent in a multi-chat world). Roster read is the GLOBAL
  // /teams/agents SWR key: N panels dedupe to one fetch, and pause/resume
  // revalidates the same key so every panel's icon updates together.
  const { read: readTeams } = useTeamsApi();
  const { data: teamsAgentsData } = readTeams<{ agents?: { id: string; status?: string | null }[] }>(
    "/agents",
  );
  const { pause, resume } = useAgentMutations();
  const paused =
    teamsAgentsData?.agents?.find((a) => a.id === resolvedId)?.status === "paused";
  const [busy, setBusy] = useState(false);

  const title = useMemo(() => {
    const agent = agents.find((a) => a.id === resolvedId);
    return agent ? agentDisplayName(agent) : raw === DEFAULT_AGENT_SENTINEL ? "Chat" : raw;
  }, [agents, resolvedId, raw]);

  // Keep the dockview tab title in sync with the resolved agent name.
  if (props.api.title !== title) props.api.setTitle(title);

  if (agentGone) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-sm text-[#8a8578]">
        <p>This agent no longer exists.</p>
        <button
          className="rounded border border-[#e0dbd0] px-3 py-1 hover:bg-[#f3efe6]"
          onClick={() => props.api.close()}
        >
          <X size={14} className="mr-1 inline" /> Close panel
        </button>
      </div>
    );
  }

  async function togglePause(): Promise<void> {
    if (!resolvedId || busy) return;
    setBusy(true);
    try {
      if (paused) await resume(resolvedId);
      else await pause(resolvedId);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-[#e0dbd0] bg-[#faf7f2] px-2">
        <span className="truncate text-sm font-medium">{title}</span>
        <div className="flex-1" />
        {resolvedId && (
          <>
            <button title="Inspect agent" onClick={() => openPanel("agent-inspector", { agent: resolvedId }, { pushUrl: true })}>
              <SlidersHorizontal size={16} />
            </button>
            <button title="Agent tasks" onClick={() => openPanel("teams-issues", { agent: resolvedId }, { pushUrl: true })}>
              <ListTodo size={16} />
            </button>
            <button title={paused ? "Resume agent" : "Pause agent"} disabled={busy} onClick={togglePause}>
              {paused ? <Play size={16} /> : <Pause size={16} />}
            </button>
          </>
        )}
      </div>
      <div className="min-h-0 flex-1">
        <AgentChatWindow
          agentId={resolvedId}
          onOpenFile={(path) => openPanel("file-viewer", path ? { path } : {})}
        />
      </div>
    </div>
  );
}
```

Requires `useAgents` to expose `isSettled: boolean` — add to `src/hooks/useAgents.ts` return: `isSettled: data !== undefined` (one line; existing consumers unaffected).

**ChatInput focus-composer targeting** (spec §4; every mounted ChatInput listens today, so N docked composers race):

In `src/components/chat/ChatInput.tsx`, the listener at :130-153 changes to:

```ts
useEffect(() => {
  function onFocusComposer(e: Event) {
    const detail = (e as CustomEvent<{ agentId?: string }>).detail;
    // Targeted dispatch (desktop dock): only the matching agent's composer
    // responds. Untargeted dispatch (mobile ChatLayout): preserve today's
    // behavior — the single mounted composer focuses.
    if (detail?.agentId && detail.agentId !== agentIdRef.current) return;
    // …existing rAF retry-focus loop body unchanged…
  }
  window.addEventListener("getstarted:focus-composer", onFocusComposer);
  return () => window.removeEventListener("getstarted:focus-composer", onFocusComposer);
}, []);
```

(`agentIdRef` = a ref mirroring the component's agentId prop — check ChatInput's existing props; if agentId isn't threaded to ChatInput, thread it from AgentChatWindow, optional so mobile is unaffected.)

**TeamsDockPanel** — per-panel workspace-status gate (the status probe is the expensive part; SWR dedupes the `/teams/dashboard` key across N panels, verified):

```tsx
// src/components/workspace/panels/TeamsDockPanel.tsx
"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { useEffect, useState } from "react";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";
import { useTeamsWorkspaceStatus } from "@/hooks/useTeamsApi";
import { ProvisioningOverlay, SubscribeOverlay, ErrorOverlay } from "@/components/teams/TeamsStatusOverlays";

interface TeamsDockPanelParams {
  teamsKey: string;      // "inbox" | "issues" | … | "issue-detail" | …
  detailId?: string;
  agent?: string;        // durable issues filter
  new?: string;          // one-shot, re-delivered via updateParameters
  newIssue?: string;     // one-shot
}

export function TeamsDockPanel(props: IDockviewPanelProps<TeamsDockPanelParams>): React.ReactElement {
  const status = useTeamsWorkspaceStatus();
  // One-shots re-deliver via updateParameters on an already-open panel —
  // subscribe to parameter changes so a second ?new=task command re-fires.
  const [params, setParams] = useState(props.params);
  useEffect(() => {
    const d = props.api.onDidParametersChange((next) => setParams(next as TeamsDockPanelParams));
    return () => d.dispose();
  }, [props.api]);

  if (status.state === "provisioning") return <ProvisioningOverlay />;
  if (status.state === "subscribe") return <SubscribeOverlay />;
  if (status.state === "error") return <ErrorOverlay />;

  const clearOneShot = (key: "new" | "newIssue") => props.api.updateParameters({ [key]: undefined });

  return (
    <div className="editorial-page h-full overflow-auto">
      <TeamsPanelRouter
        panel={params.teamsKey}
        detailId={params.detailId ?? null}
        panelParams={{
          ...(params.agent ? { agent: params.agent } : {}),
          ...(params.new ? { new: params.new } : {}),
          ...(params.newIssue ? { newIssue: params.newIssue } : {}),
        }}
        onConsumeOneShot={clearOneShot}
        onUpdateDurableParam={(key, value) =>
          props.api.updateParameters({ [key]: value ?? undefined })
        }
      />
    </div>
  );
}
```

NOTE: match `useTeamsWorkspaceStatus`'s REAL return shape when implementing — read `src/hooks/useTeamsApi.ts:158-184` and `InlineTeamsPanel.tsx:39-42` first and mirror InlineTeamsPanel's overlay conditions exactly (the shape above is illustrative; the gate conditions are the contract). `updateParameters({ key: undefined })` deletes the key (documented dockview behavior) — one-shots therefore never persist into `toJSON()`.

**ControlDockPanel** — same host pattern wrapping `ControlPanelRouter` with `controlKey`/`agentId`/`initialTab`/`action` params and `onConsumeOneShot={(k) => props.api.updateParameters({ [k]: undefined })}`.

**FileViewerDockPanel** — wraps the existing `FileViewer` (`{ agentId, initialFilePath, onClose }`): agentId = focused chat agent from `useWorkspaceState().focusedChatAgentId` falling back to resolved default; `initialFilePath` from `props.params.path ?? null`; `onClose={() => props.api.close()}`; subscribe to `onDidParametersChange` so opening a file path into the already-open viewer updates it.

**MissingPanel** — `"This panel is no longer available."` + close button (`props.api.close()`).

**panelComponents.ts** — the module-scope map (identity-stable; the components prop is watched):

```ts
// src/components/workspace/panelComponents.ts
import type { IDockviewPanelProps } from "dockview-react";
import { ChatPanel } from "./panels/ChatPanel";
import { TeamsDockPanel } from "./panels/TeamsDockPanel";
import { ControlDockPanel } from "./panels/ControlDockPanel";
import { FileViewerDockPanel } from "./panels/FileViewerDockPanel";
import { MissingPanel } from "./panels/MissingPanel";

export const PANEL_COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  chat: ChatPanel as React.FunctionComponent<IDockviewPanelProps>,
  teams: TeamsDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  control: ControlDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  "file-viewer": FileViewerDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  missing: MissingPanel as React.FunctionComponent<IDockviewPanelProps>,
  unknown: MissingPanel as React.FunctionComponent<IDockviewPanelProps>, // deserializer default key
};
```

- [ ] **Step 1: Write failing ChatPanel tests** — sentinel resolution (defaultId arrives → title becomes agent name, AgentChatWindow receives resolved id); agent-gone renders ONLY when `isSettled` (mock useAgents: `{ agents: [], defaultId: undefined, isSettled: false }` → normal chat shell, `isSettled: true` + unknown id → gone-state); action cluster calls openPanel with pushUrl. Mock AgentChatWindow, useTeamsApi, useAgentMutations, workspaceController.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement all five panels + the map + the useAgents.isSettled line + ChatInput targeting**
- [ ] **Step 4: Run new tests + the existing ChatInput/AgentChatWindow suites single-file; tsc; lint**
- [ ] **Step 5: Commit**

```bash
git add src/components/workspace src/hooks/useAgents.ts src/components/chat/ChatInput.tsx
git commit -m "feat(workspace): dock panel hosts — chat (sentinel/agent-gone/actions), teams, control, files"
```

---

### Task 10: WorkspaceSidebar + WorkspaceHeader (desktop chrome)

**Files:**
- Create: `src/components/workspace/WorkspaceSidebar.tsx`
- Create: `src/components/workspace/WorkspaceHeader.tsx`
- Create: `src/components/workspace/workspace.css` (shell grid: fixed 280px sidebar + header row + dock area; reuse ChatLayout.css palette values: cream `#f3efe6`/`#faf7f2`, border `#e0dbd0`, forest `#06402B`)
- Test: `src/components/workspace/__tests__/WorkspaceSidebar.test.tsx`

**Interfaces:**
- Consumes: `useWorkspaceState().focusedChatAgentId` (active-row highlight), `openChatPanel`/`openPanel` from the controller; the exact component contracts from ChatLayout: `Monogram`/`StatusDot` from `@/components/agents/AgentIdentity`, `SessionList { sessions, activeSessionId, onSelect, onNew, onRename?, onDelete? }`, `CompanyNav { activePanel?, onPanelChange? }`, `GallerySection` (zero props, needs MarketplaceDrawerProvider), `HealthIndicator { onRecoveryReprovision? }`, `DesktopUpdater { version? }`, `useAgentSessions(agentId, userId)`, `useActiveSession(agentId)`, dialogs from `@/components/chat/AgentDialogs`.
- Produces:

```ts
interface WorkspaceSidebarProps {
  onRecoveryReprovision: () => void;   // wires HealthIndicator → shell's recovery state
}
// WorkspaceHeader takes no props (file button → openPanel("file-viewer"), support → useSupport, UserButton)
```

Build the sidebar as a port of ChatLayout's `.cream-sidebar` block (ChatLayout.tsx:275-483) with these deltas — everything else copies verbatim (JSX + class names, reusing ChatLayout.css classes where they aren't grid-coupled):
1. **Active agent** = `useWorkspaceState().focusedChatAgentId` (NOT a `userSelectedId` state — the dock owns focus). Roster row click → `openChatPanel(agent.id)`; also `capture("agent_selected", { agent_id })`.
2. **No auto-dispatch effect** (ChatLayout:174-180 fights restored layouts — the default layout already opens the sentinel chat).
3. **No mobile drawer state** (sidebarOpen/backdrop/hamburger are mobile chrome; desktop sidebar is fixed).
4. SessionList renders under the row whose id === focusedChatAgentId (same as today's currentAgentId nesting); onSelect/onNew keep `useActiveSession` semantics (module store — the focused ChatPanel's transcript follows automatically).
5. CompanyNav: `activePanel` = `useWorkspaceState().activePanelId` when it's a teams id else undefined; `onPanelChange={(key) => openPanel(key, {}, { pushUrl: true })}`.
6. Create pill: Agent → `AgentCreateDialog` (state local to sidebar); Task → `openPanel("teams-issues", {}, { pushUrl: true, oneShots: { new: "task" } })`.
7. Per-row actions (issues/inspector/rename/delete) → `openPanel("teams-issues", { agent: id }, { pushUrl: true })` / `openPanel("agent-inspector", { agent: id }, { pushUrl: true })` / local dialog state. Dialogs (`AgentCreateDialog`/`AgentRenameDialog`/`AgentDeleteDialog`/`SessionDeleteDialog`) move INTO the sidebar (they're roster-owned).
8. On agent delete: if a `chat:<id>` panel exists, close it via the controller (add `closeChatPanelIfOpen(agentId)` helper to workspaceController: `_api?.getPanel(chatPanelId(agentId))` → `removePanel` inside a shell-op guard).

- [ ] **Step 1: Failing test** — roster click calls openChatPanel; active row derives from the store snapshot; CompanyNav change routes through openPanel with pushUrl. (Mock the controller module and every data hook; assert wiring, not visuals.)
- [ ] **Step 2: Verify failure**
- [ ] **Step 3: Implement sidebar + header + css**
- [ ] **Step 4: Tests + tsc + lint**
- [ ] **Step 5: Commit** — `feat(workspace): desktop sidebar + header chrome`

---

### Task 11: WorkspaceShell — assembly, reconciler, persistence

**Files:**
- Create: `src/components/workspace/WorkspaceShell.tsx`
- Test: `src/components/workspace/__tests__/workspaceShellUrl.test.ts` (reconciler + invariant, pure-logic level)

**Interfaces:**
- Consumes: everything above; `useOnboardingGate`, `ProvisioningStepper { children, trigger? }`, `TrialBanner`, `OutOfCreditsBanner`, `useCheckoutReturn`, `useUserSyncOnce`, `useSellerPublishedAgents`, `useGetStartedDerived` + `GetStarted { derived, actions }`, `useProvisioningState`, `InlineTeamsNavProvider`, `TeamsEventsProvider`, `CommandPaletteProvider`, `preloadTeamsPanels`, `useAuth`/`useOrganization` (scope id), `themeLight` + `DockviewReact` + CSS import.
- Produces: `WorkspaceShell(): React.ReactElement` — the entire desktop experience, mounted by page.tsx inside `MarketplaceDrawerProvider > GatewayProvider > ExecApprovalsProvider`.

**Structure (write exactly this skeleton; fill the marked bodies from the contracts below):**

```tsx
// src/components/workspace/WorkspaceShell.tsx
"use client";

import "dockview-react/dist/styles/dockview.css";
import "./workspace.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth, useOrganization } from "@clerk/nextjs";
import { DockviewReact, themeLight, type DockviewReadyEvent, type DockviewTheme } from "dockview-react";
import { useOnboardingGate } from "@/hooks/useOnboardingGate";
import { useAgents } from "@/hooks/useAgents";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import { replaceSearchParams } from "@/lib/shellNav";
import { capture } from "@/lib/analytics";
import { ProvisioningStepper } from "@/components/chat/ProvisioningStepper";
import { TrialBanner } from "@/components/chat/TrialBanner";           // ← verify import paths from ChatLayout's imports
import { OutOfCreditsBanner } from "@/components/chat/OutOfCreditsBanner";
import { GetStarted } from "@/components/getstarted/GetStarted";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
import { TeamsEventsProvider } from "@/components/teams/TeamsEventsProvider";
import { CommandPaletteProvider } from "@/components/teams/command-palette/CommandPaletteProvider";
import { preloadTeamsPanels } from "@/components/teams/TeamsPanelRouter";
import { useUserSyncOnce } from "@/components/chat/shell/useUserSyncOnce";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { useGetStartedDerived } from "@/components/chat/shell/useGetStartedDerived";
import { PANEL_COMPONENTS } from "./panelComponents";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { buildMissionControlLayout } from "./buildDefaultLayout";
import { readLayout, writeLayout } from "./layoutStorage";
import { parseCommand, commandSignature, stripOneShots, clearPanelFromUrl } from "./urlChannel";
import {
  attachDockviewApi,
  openPanel,
  openChatPanel,
  isShellOperationInProgress,
  resetWorkspaceLayout,
} from "./workspaceController";

const ISOL8_THEME: DockviewTheme = { ...themeLight, name: "isol8", gap: 4 };

export function WorkspaceShell(): React.ReactElement {
  const onboardingPhase = useOnboardingGate();       // exactly ONE mount (router side effects)
  const { userId, isSignedIn } = useAuth();
  const { organization } = useOrganization();
  const scopeId = organization?.id ?? userId ?? "anon"; // gate below makes "anon" unreachable
  const { defaultId } = useAgents();
  const defaultIdRef = useRef<string | undefined>(defaultId);
  defaultIdRef.current = defaultId;

  const searchParams = useSearchParams();
  const lastAppliedRef = useRef<string | null>(null);
  const [recoveryTriggered, setRecoveryTriggered] = useState(false);

  useUserSyncOnce(Boolean(isSignedIn) && onboardingPhase === "ready");
  const { showSubscriptionSuccess } = useCheckoutReturn();
  const { container } = useProvisioningState();
  const chatReady = container?.phase === "ready";
  const getStartedDerived = useGetStartedDerived(chatReady);

  // [BODY 1: persistence] — see contract below
  // [BODY 2: onReady]
  // [BODY 3: reconciler effect]
  // [BODY 4: selectAgent listener + teams idle preload + GetStarted actions]

  if (onboardingPhase !== "ready") {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
        <div style={{ color: "#6b6b6b", fontSize: 14 }}>Loading…</div>
      </div>
    );
  }

  return (
    <InlineTeamsNavProvider
      value={{
        openDetail: (panelKey, id) =>
          openPanel(`teams-${panelKey}`, { detailId: id }, { pushUrl: true }),
        leaveToList: (listPanelKey) => {
          /* desktop impl: closeSubjectDeleted from the calling panel — the
             detail panels call leaveToList with their own list key; resolve
             the calling panel id from the workspace snapshot's activePanelId */
        },
      }}
    >
      <TeamsEventsProvider>
        <CommandPaletteProvider>
          <div className="workspace-shell">{/* grid: sidebar | (header + main) */}
            <WorkspaceSidebar onRecoveryReprovision={() => setRecoveryTriggered(true)} />
            <div className="workspace-main">
              <WorkspaceHeader />
              <div className="workspace-content">
                <TrialBanner />
                <OutOfCreditsBanner />
                {showSubscriptionSuccess && (
                  <div className="subscription-banner">Subscription confirmed! Your agent is being upgraded.</div>
                )}
                {recoveryTriggered ? (
                  <ProvisioningStepper trigger="recovery">{/* dock */}</ProvisioningStepper>
                ) : (
                  <ProvisioningStepper>{/* same dock element — prop-switch ternary, NO key */}</ProvisioningStepper>
                )}
              </div>
            </div>
            {chatReady && <GetStarted derived={getStartedDerived} actions={/* BODY 4 */} />}
          </div>
        </CommandPaletteProvider>
      </TeamsEventsProvider>
    </InlineTeamsNavProvider>
  );
}
```

The dock element (shared by both stepper branches — extract to a variable):

```tsx
const dock = (
  <div className="workspace-dock">{/* size-constrained wrapper: flex:1 min-height:0 — dockview fills its parent */}
    <DockviewReact components={PANEL_COMPONENTS} theme={ISOL8_THEME} onReady={onReady} disableFloatingGroups />
  </div>
);
```

**BODY contracts:**

1. **Persistence:** in `onReady`, after restore, subscribe `event.api.onDidLayoutChange` → debounced (500 ms, `setTimeout` ref) `writeLayout(scopeId, api.toJSON())`. Flush function (clear timer + immediate write) registered on `pagehide` and `visibilitychange` (only when `document.visibilityState === "hidden"`), and called in the effect cleanup that runs on dock unmount (recovery regression unmounts the dock mid-session). All listeners disposed on cleanup.
2. **onReady(event: DockviewReadyEvent):** `const api = event.api;` → `const detach = attachDockviewApi(api, () => defaultIdRef.current);` (keep `detach` in a ref, call on unmount) → restore: `const saved = readLayout(scopeId); if (saved) { try { api.fromJSON(saved); } catch { api.clear(); buildMissionControlLayout(api); } } else { buildMissionControlLayout(api); }` → then execute the current URL command on top (call the reconciler body once with `searchParams` — URL wins focus, spec §6 rule 4) → subscribe `api.onDidRemovePanel((panel) => { if (isShellOperationInProgress()) return; const cleared = clearPanelFromUrl(window.location.search, panel.id, /* urlPanelKey */ new URLSearchParams(window.location.search).get("panel") ?? ""); if (cleared !== null) replaceSearchParams(new URLSearchParams(cleared)); })` → wire persistence (contract 1).
3. **Reconciler effect** (`useEffect` on `[searchParams]`): `const sig = commandSignature(searchParams.toString()); if (sig === lastAppliedRef.current) return; lastAppliedRef.current = sig; const cmd = parseCommand(searchParams.toString()); if (!cmd) return; if (cmd.descriptor.component === "chat") { openChatPanel(cmd.descriptor.params.agentId); } else { openPanel(/* reconstruct panelKey from the raw ?panel value after alias resolution */, /* raw durable params */, { oneShots: cmd.oneShots }); } const stripped = stripOneShots(searchParams.toString()); if (stripped !== null) replaceSearchParams(new URLSearchParams(stripped));`. NOTE: `openPanel` here must NOT pushUrl (the URL already carries the command). Guard ordering: signature latch BEFORE parse so the strip's searchParams churn (which changes the string but not the signature — one-shots ARE part of the signature, so recompute `lastAppliedRef.current = commandSignature(stripped)` after stripping) doesn't re-execute. This ordering subtlety is exactly what `workspaceShellUrl.test.ts` pins.
4. **selectAgent listener:** `window.addEventListener("selectAgent", (e) => { const detail = (e as CustomEvent<{ agentId?: string }>).detail; if (detail?.agentId) openChatPanel(detail.agentId); })` — the shell only LISTENS, never dispatches (loop-free by construction; accepts sourceless MarketplaceToast events AND RunNotifications' `source: "notification"`). **Teams idle preload:** copy page.tsx:135-143 verbatim. **GetStarted actions:** `chat: () => { const target = defaultIdRef.current; if (target) { openChatPanel(target); window.dispatchEvent(new CustomEvent("getstarted:focus-composer", { detail: { agentId: target } })); } }`, `task: () => openPanel("teams-inbox", {}, { pushUrl: true, oneShots: { newIssue: "onboarding" } })`, `channel: () => { const target = defaultIdRef.current; if (target) openPanel("agent-inspector", { agent: target, inspectorTab: "channels" }, { pushUrl: true }); }`, `hire: () => openMarketplace({ view: "browse" })` (via `useMarketplaceDrawer()`); also move ChatLayout's freshly-installed `emitStep("hire")` effect (ChatLayout:186-193) here (desktop) — it stays in ChatLayout too (mobile); both are gated by their shell so exactly one runs.
   Also add analytics: `capture("workspace_panel_opened", { panel })` inside openPanel's push path... — NO: analytics lives HERE in the shell (controller is UI-free): capture in the reconciler when a command opens a NEW panel, and `capture("workspace_layout_reset")` in the Reset handler. Add both event names to the `AnalyticsEvent` union (`src/lib/analytics.ts` ~:62, alphabetical).
   **Reset layout** button (small control in WorkspaceHeader): `resetWorkspaceLayout(buildMissionControlLayout)` + `clearLayout(scopeId)` + capture.

`oneShots` re-delivery through `openPanel(..., { oneShots })` reaches already-open panels via `updateParameters` (controller Task 6) and new panels via merged open params — TeamsDockPanel/ControlDockPanel clear them after consumption (Task 9), so they never persist.

- [ ] **Step 1: Write the URL-logic test** (pure, no dockview render — extract the reconciler decision into a tiny exported helper `deriveReconcilerAction(search, lastSig)` if needed for testability):

```ts
// src/components/workspace/__tests__/workspaceShellUrl.test.ts — pins:
// 1. same signature twice → second call is a no-op (lastApplied latch)
// 2. one-shot strip recomputes the latch so the strip doesn't re-execute
// 3. billing-return params never form a command
// 4. INVARIANT: the panel-focus path performs zero history writes — assert
//    that pushSearchParams/replaceSearchParams are NOT called by
//    attach/focus flows (mock shellNav; drive the controller's addOrFocus
//    directly via openPanel twice; only pushUrl:true calls may write).
```

- [ ] **Step 2: Verify failure** — **Step 3: Implement the shell** — **Step 4: `pnpm vitest run` the new test + tsc + lint + `pnpm run build`** — **Step 5: Commit** `feat(workspace): WorkspaceShell — dock host, URL reconciler, persistence, chrome assembly`

---

### Task 12: RunNotifications multi-panel suppression

**Files:**
- Modify: `src/components/notifications/runToastReducer.ts` (RunToastContext gains `watchedChatKeys`)
- Modify: `src/components/notifications/RunNotifications.tsx` (desktop ctx from the workspace store)
- Modify: `src/app/chat/page.tsx` (props per shell — see Task 13)
- Tests: extend `src/components/notifications/__tests__/runToastReducer.test.ts` + `RunNotifications.test.tsx`

**Interfaces:**
- `RunToastContext` (runToastReducer.ts:12-17) changes to:

```ts
export interface RunToastContext {
  watchedChatKeys: ReadonlySet<string>; // `${agentId}` and `${agentId}:${sessionId}` entries the user is watching
  teamsPanelActive: boolean;
}
```

- `chatSuppressed` (currently :47-53, comparing selectedAgentId/selectedSessionId/chatViewActive) becomes: suppressed iff `watchedChatKeys.has(agentId)` (agent-level match when the frame has no session) or `watchedChatKeys.has(`${agentId}:${sessionId}`)`. Mobile builds the set from `selectedAgentId`+`activeSessionId`+`chatViewActive` (0-or-2 entries — byte-equivalent behavior to today); desktop builds it from EVERY visible chat panel: for each dockview group, its active panel if it's a chat panel → resolved agentId (+ its `useActiveSession` session) — spec §3: "a chat run is suppressed iff that agent's chat panel is the active tab of a visible group".
- `RunNotificationsProps` becomes `{ mode: "mobile"; selectedAgentId: string | null; activeView: "chat" | "control"; activePanel: string } | { mode: "desktop" }` (discriminated union). Desktop mode reads `useWorkspaceState()` + a new controller snapshot field `visibleChatAgentIds: string[]` (active panel per group — extend `recomputeSnapshot` in Task 6's controller using `api.groups[].activePanel`; add to the fake-api test).

- [ ] **Step 1: Failing reducer tests** — visible-but-not-focused chat panel suppresses its agent's toast; unrelated agent still toasts; teams suppression unchanged. Update ALL existing reducer tests' ctx construction to the new shape (mechanical: `{ selectedAgentId: "a", selectedSessionId: "s", chatViewActive: true }` → `{ watchedChatKeys: new Set(["a", "a:s"]), teamsPanelActive: … }`).
- [ ] **Step 2: Verify failure** — **Step 3: Implement** — **Step 4: Run both notification suites + tsc** — **Step 5: Commit** `feat(notifications): multi-panel watched-keys suppression context`

---

### Task 13: page.tsx integration — desktop/mobile branch

**Files:**
- Modify: `src/app/chat/page.tsx`
- Test: `src/__tests__/app/chatPageBranch.test.tsx` (new)

**Interfaces:**
- Consumes: `useIsDesktop`, `WorkspaceShell`, everything mobile already uses.
- Produces: the final page composition:

```tsx
function ChatPageInner() {
  const isDesktop = useIsDesktop();
  return (
    <MarketplaceDrawerProvider>
      <GatewayProvider>
        <ExecApprovalsProvider>
          {isDesktop ? <WorkspaceShell /> : <MobileChatShell />}
        </ExecApprovalsProvider>
        <MarketplaceDrawer />
        <MarketplaceToast />
        {isDesktop ? <RunNotifications mode="desktop" /> : null /* mobile mounts inside MobileChatShell wrapper below */}
      </GatewayProvider>
    </MarketplaceDrawerProvider>
  );
}
```

- `MobileChatShell` = a new local component in page.tsx holding EVERYTHING the page does today verbatim: the param derivation (:40-43), nav helpers (:52-130), selectedAgentId state + selectAgent listener (:145-170), file viewer state, ChatLayout composition with children, the Task 8 mobile param threading, `leaveToList`, and the mobile-mode `<RunNotifications mode="mobile" selectedAgentId… />` mount (moved inside so its props stay colocated with the state that feeds them — it must remain INSIDE GatewayProvider: mount it as a sibling of ChatLayout within MobileChatShell's fragment).
- The desktop branch renders NO ChatLayout, NO page-level selectedAgentId, NO nav helpers — WorkspaceShell owns all of it.
- Teams idle preload: lives in BOTH branches (WorkspaceShell has it from Task 11; MobileChatShell keeps the existing effect).
- Breakpoint crossing remounts the other shell (spec §7): chat state survives via module stores + `_messageCache`; the layout blob persists the dock. No extra work — but the RunNotifications mount swaps too; its refcounted acquire/release handles this (release on unmount → acquire on mount; refcount dips 1→0→1 sending an unsubscribe+resubscribe pair — acceptable; note it in the PR body).

- [ ] **Step 1: Failing branch test** — mock `useIsDesktop`: true → WorkspaceShell rendered (mock it), no ChatLayout; false → ChatLayout path rendered, WorkspaceShell absent.
- [ ] **Step 2: Verify failure** — **Step 3: Implement** — **Step 4: New test + tsc + lint + `pnpm run build`** — **Step 5: Commit** `feat(chat): desktop workspace shell replaces classic layout; ChatLayout is the mobile shell`

---

### Task 14: Carry-forwards — useChannelLinks hoist + chatReady inside useGetStartedDerived + stale comments

**Files:**
- Create: `src/hooks/useChannelLinks.ts` (the ONE fetcher; response type exported)
- Modify: `src/components/chat/shell/useGetStartedDerived.ts` (consume the hook; derive chatReady INTERNALLY via `useProvisioningState()`; signature becomes `useGetStartedDerived(): GetStartedDerivedSignals & { chatReady: boolean }` — ChatLayout + WorkspaceShell both drop their local derivation and read `.chatReady` from the hook, keeping the 20-line phase-rationale comment IN the hook)
- Modify: `src/components/settings/MyChannelsSection.tsx:30` + `src/components/control/panels/AgentChannelsSection.tsx:29` (consume `useChannelLinks`, delete their private `LinksMeResponse` copies)
- Modify: `src/components/chat/ChatLayout.tsx` (drop chatReady derivation, fix the two stale cross-reference comments flagged in PR-1 review — "roster fetch above" / "sibling seller-listings read")
- Modify: `src/components/workspace/WorkspaceShell.tsx` (same signature change)
- Tests: `src/hooks/__tests__/useChannelLinks.test.tsx` (new); existing `useGetStartedDerived` tests updated for the new signature.

Contract: `useChannelLinks(enabled: boolean): { channelLinks: ChannelLinksResponse | undefined; channelLinked: boolean }` — SWR key `/channels/links/me` null-gated on `enabled`, options exactly today's (`revalidateOnFocus: false, dedupingInterval: 300_000`); `channelLinked` = the existing `.some((b) => b.linked)` derivation over telegram/discord/slack.

- [ ] Steps: failing hook test → implement → migrate 3 consumers → run touched suites + tsc + build → commit `refactor(channels): single useChannelLinks hook; chatReady derived inside useGetStartedDerived`

---

### Task 15: Carry-forwards — shared permissions subscribe + chat.history hardening

**Files:**
- Create: `src/lib/desktopPermissionsSubscribe.ts`:

```ts
// Shared cross-tab + same-tab subscription to desktop-permission changes.
// The two-listener pattern (custom event + key-filtered storage event) was
// duplicated in useGateway and DesktopPermissionsGate; PR-2's layout blob
// makes an unfiltered listener expensive, so consumers must go through here.
import {
  DESKTOP_PERMISSIONS_CHANGED_EVENT,
  isPermissionsStorageEvent,
} from "./desktopPermissions";

export function subscribeDesktopPermissions(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (isPermissionsStorageEvent(event)) onChange();
  };
  window.addEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, onChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, onChange);
    window.removeEventListener("storage", onStorage);
  };
}
```

- Modify: `src/hooks/useGateway.tsx:79-91` + `src/components/desktop/DesktopPermissionsGate.tsx:44-55` to consume it (delete both inline copies).
- Modify: `src/hooks/useAgentChat.ts` — two hardening fixes from the PR-1 review:
  (a) **in-flight duplicate guard**: latch `historyLoadedRef.current.add(cacheKey)` BEFORE `sendReq` (not in `.then`), and un-latch in `.catch` so a failed fetch retries on the next effect run — kills the double-RPC on isConnected flicker/StrictMode;
  (b) **authoritative-empty reconcile**: in the empty-`projected` branch, when `hasCache` is true AND the server result was a clean success, DELETE the stale cache (`_messageCache.delete(cacheKey)`) and clear the live transcript if this session is still active — a session cleared server-side (chat.clear elsewhere, reprovision) must stop showing ghost history. Bump `transcriptMutationSeqRef` rules do NOT apply here (this IS server truth) — but keep the seq guard check: if local mutations happened after issue, skip (the run in flight owns the transcript).
- Tests: `src/hooks/__tests__/useAgentChat.historyRevalidate.test.tsx` — add: (1) two rapid effect runs → exactly one chat.history call; (2) failed fetch → retry possible on next run; (3) cached transcript + authoritative-empty server → cache cleared, bootstrap flagged; (4) authoritative-empty + local send in flight → cache kept. Update the two existing tests if the latch move shifts call counts.
- Also: `src/components/chat/__tests__/ChatLayout.test.tsx:58-59` — fix the stale "mounted by ChatLayout" comment (PR-1 carry-forward).

- [ ] Steps: failing tests → implement → run historyRevalidate + cacheIntegrity + queue + connectRace suites single-file + tsc → commit `fix(chat): dedupe in-flight history fetches; reconcile authoritative-empty sessions; shared permissions subscribe`

---

### Task 16: Carry-forward — scoped-hotkey utility (Cmd+S)

**Files:**
- Create: `src/hooks/useScopedHotkey.ts`
- Modify: `src/components/chat/FileContentViewer.tsx` (replace the hand-rolled listener + 6 ref attachments from PR-1 with the hook, ref on the top-level wrapper each branch already shares — check the current branch structure first)
- Test: `src/hooks/__tests__/useScopedHotkey.test.tsx` (port the two assertions from `FileContentViewer.cmdS.test.tsx`: inside fires / outside doesn't; plus: two instances, one keystroke → only the focus-owning instance fires)

Contract:

```ts
export function useScopedHotkey(
  ref: React.RefObject<HTMLElement | null>,
  matcher: (e: KeyboardEvent) => boolean,   // e.g. (e) => (e.metaKey || e.ctrlKey) && e.key === "s"
  handler: () => void,
  enabled = true,
): void;
// window keydown; fires handler (with preventDefault) only when enabled AND
// ref.current.contains(document.activeElement). One listener per instance,
// scoped by focus — the multi-panel-safe replacement for global hotkeys.
```

Keep the existing `FileContentViewer.cmdS.test.tsx` green unchanged — the refactor must preserve its contract (including the `.lobster` Tabs-root case).

- [ ] Steps: failing hook test → implement hook → refactor FileContentViewer → run both suites + tsc → commit `refactor(chat): shared focus-scoped hotkey hook for Cmd+S`

---

### Task 17: Analytics + spec doc sync

**Files:**
- Modify: `src/lib/analytics.ts` (~:62): add `"workspace_layout_reset" | "workspace_panel_opened"` to the union (alphabetical position; Task 11 already captures them — this task exists only if Task 11 deferred the union edit; fold it there if not).
- Modify: `docs/superpowers/specs/2026-07-11-dockable-workspace-design.md`: record the two implementation-driven deviations — (1) Teams providers (nav/events/palette) mount unconditionally above the dock (RunNotifications already holds a permanent teams.subscribe refcount, so conditional mounting saved nothing; the expensive status probe is gated per teams panel instead); (2) legacy control panel keys (`llm`, `agents`, …) open as dock panels for URL grammar parity (rule 9) even though they're not sidebar surfaces.
- [ ] Steps: edit → tsc → commit `docs(workspace): record provider-mount + control-key grammar deviations`

---

### Task 18: Final gates + manual verification + STOP for push approval

- [ ] **Step 1: Full local gate set**

```bash
pnpm exec tsc --noEmit && pnpm run lint && pnpm run build
# Single-file runs of every suite this PR created or touched (list them explicitly from the ledger)
```

- [ ] **Step 2: Manual dev checklist** (`pnpm run dev`, desktop viewport):
  - First load (no blob): Mission Control appears — sentinel chat resolves to Friday once the roster lands; Tasks + Inbox right.
  - Drag Tasks onto Inbox (tab stack), resize gutters, reload → layout restored exactly; focus restored.
  - Sidebar: click a second agent → new chat panel; click Friday → focuses the sentinel panel (no duplicate).
  - Two chats visible, run in the non-focused one → NO toast (visible = watched); run in a closed agent → toast; toast View → opens/focuses that chat panel.
  - `?panel=teams-issues&new=task` pasted → Tasks opens + create dialog; URL loses `new=` immediately; reload does NOT re-open the dialog.
  - Close the URL-named panel → URL canonicalizes (no push); refresh does not resurrect it.
  - Back/forward walks explicit navigations; focus clicks create no entries.
  - ChatErrorCard "Switch provider" (`?panel=llm&action=change-provider`) opens the LLM dock panel with the modal; Cmd+K palette navigates panels.
  - File button → Files panel; open file from chat message path → Files panel focuses with the file.
  - Delete a task from its detail panel → panel closes, Tasks list focused, URL = teams-issues (replace).
  - Reset layout → Mission Control; narrow the window <769px → mobile ChatLayout appears, chat transcript intact; widen → workspace restores.
  - Console clean throughout.
- [ ] **Step 3: Ledger + STOP.** Update `.superpowers/sdd/progress.md`. Then STOP and ask the user for approval to push `feat/dockable-workspace-shell` and open the PR (title `feat(chat): dockable workspace shell — dockview panels, layout persistence, URL command channel`; body: summary, the two spec deviations, the breakpoint-crossing resubscribe note, carry-forwards completed list, standard footer). Do NOT push without approval.

---

## Self-review notes

- **Spec coverage:** §3 architecture (T11+T13), §4 panels/registry (T3/T8/T9), §5 persistence (T4, flush in T11), §6 URL rules 1-9 (T5/T6/T11; rule 8 delete-API in T6+T8; rule 7 `?agent=` in T5), §7 error handling (sanitizer T4, agent-gone T9, recovery unmount flush T11, breakpoint T13), §8 testing (per-task + the zero-history-writes invariant in T11), §9 fast-follow carry-forwards (T14-T16). Presets/copy-link/run-badges remain OUT (spec fast-follows).
- **Known deliberate deviations** recorded in T17.
- **Type consistency check:** `describePanel` params are `Record<string, string>` end-to-end; controller's `openPanel(panelKey, params, opts)` matches every call site shown in T10/T11; `RunToastContext.watchedChatKeys` construction shown for both shells (T12/T13); `useGetStartedDerived()` signature change (T14) touches exactly ChatLayout + WorkspaceShell + its own tests.
- **Deliberate adaptation points (not placeholders):** T9's TeamsDockPanel gate conditions defer to the REAL `useTeamsWorkspaceStatus` shape (named file+lines to read); T10 ports a named JSX block with an explicit delta list; T11's BODY contracts specify exact behavior while letting the implementer lay out the function. Every such point names its source of truth.
