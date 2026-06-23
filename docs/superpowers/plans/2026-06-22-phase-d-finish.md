# Phase D Finish — /chat sidebar cleanup + Settings polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Finish the unified-nav Phase D: remove the wrong `SYSTEM` sidebar section, restore the agents roster on `/chat`, and fix the Settings page (Connections route dropping the nav, spacing, MCP/Connections consistency).

**Architecture:** All changes are behind the `unifiedNav` flag (dev-only). PR 1 (Settings consolidation) already merged — config panels now live in `/settings`, so the `/chat` `SystemNav` is now redundant and can be removed. The agents roster is missing because `.agent-list` (flex:1), `CompanyNav`, and `SystemNav` each have their own scroll region competing for height; with SystemNav gone and one shared scroll region, the roster regains its natural height.

**Tech Stack:** Next.js 16 App Router, React 19, Tailwind v4, plain CSS (`ChatLayout.css`), vitest.

## Global Constraints

- **Flag-OFF must be byte-identical to today's app.** Every change must be gated so `unifiedNav === false` renders exactly as before. `.agent-list` + `GallerySection` render in BOTH flag states — any restructure around them must NOT change the flag-off layout.
- Worktree: `/Users/prasiddhaparthsarthy/Desktop/isol8-phase-d-fix` (branch `worktree-phase-d-pr2`, off current `origin/main`). `cd` there; commit locally; do NOT push.
- Single-file vitest only (`npx vitest run <file>`) — NEVER the full suite (~17 GB RAM). `npx tsc --noEmit` clean + `npm run lint` 0 errors before each commit. Annotate test-mock return types (CI runs tsc over test files).
- Connections MUST stay reachable at the route `/settings/connections` (OAuth providers redirect there — a registered redirect URI). Do not convert it to a `?panel=` tab.

---

### Task 1: Remove the `SystemNav` section from the `/chat` sidebar

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx` (import line 28; render blocks ~489–491 and ~508–510; the comment at ~376–379 mentions SystemNav)
- Delete: `apps/frontend/src/components/chat/SystemNav.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/ChatLayout.systemnav-removed.test.ts` (source-level, mirrors the existing `ChatLayout.*.test.ts` `readFileSync` pattern)

**Why:** Spec §3 forbids a "System" section; PR #995 wrongly added it. Its panels now live in Settings (PR 1, merged), so it is pure redundancy.

- [ ] **Step 1: Failing source-level test** — `ChatLayout.systemnav-removed.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");
describe("ChatLayout — SystemNav removed", () => {
  it("no longer imports or renders SystemNav", () => {
    expect(source).not.toMatch(/SystemNav/);
  });
  it("still renders CompanyNav (kept)", () => {
    expect(source).toMatch(/CompanyNav/);
  });
});
```
- [ ] **Step 2: Run, expect FAIL** — `cd apps/frontend && npx vitest run src/components/chat/__tests__/ChatLayout.systemnav-removed.test.ts` → FAIL (SystemNav still present).
- [ ] **Step 3: Implement** — In `ChatLayout.tsx`: delete the `import { SystemNav } from "@/components/chat/SystemNav";` line (28); delete BOTH `{unifiedNav ? (<SystemNav … />) : null}` blocks (~489–491 and ~508–510); update the ~376–379 comment to drop the SystemNav mention. Then delete the file `apps/frontend/src/components/chat/SystemNav.tsx`. Leave `CompanyNav`, `ControlSidebar`, the agent-list, and the footer untouched.
- [ ] **Step 4: Run, expect PASS** (2 tests). Then `npx tsc --noEmit` (fix any now-unused imports it flags), `npm run lint`.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(chat): remove the System nav section from the unified sidebar — Phase D"`

---

### Task 2: One-scroll sidebar so the agents roster is visible again (flag-ON only)

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx` (chat branch body, ~389–497)
- Modify: `apps/frontend/src/components/chat/ChatLayout.css` (`.agent-list` at ~142; add `.sidebar-scroll`)
- Modify: `apps/frontend/src/components/chat/CompanyNav.tsx` (the `<nav>` class at ~71)
- Test: `apps/frontend/src/components/chat/__tests__/ChatLayout.sidebar-scroll.test.ts` (source-level)

**Interfaces (consumes):** Task 1 already removed SystemNav, so the only scroll competitors left flag-ON are `.agent-list` and `CompanyNav`.

**Root cause:** `.agent-list { flex:1; overflow-y:auto }` + `CompanyNav` `<nav class="… min-h-0 overflow-y-auto">` are two independent scroll regions inside the fixed-height `.cream-sidebar` flex column; with CompanyNav's content tall, the roster's `flex:1` collapses to ~0. Fix: one shared scroll region around roster + CompanyNav + Gallery (flag-ON), roster at natural height.

**Flag-OFF guard:** `.agent-list` + `GallerySection` also render flag-OFF (no CompanyNav there). The new `.sidebar-scroll` wrapper and the `.agent-list` overflow change MUST apply ONLY when `unifiedNav` is true, so flag-OFF stays byte-identical. Implement by making the wrapper conditional (flag-ON wraps roster+CompanyNav+Gallery in `<div className="sidebar-scroll">`; flag-OFF keeps the current flat structure), reusing a single roster render via a local `const rosterEl = (<div className="agent-list">…</div>)` to avoid duplicating the agent map.

- [ ] **Step 1: Failing source-level test** — `ChatLayout.sidebar-scroll.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");
describe("ChatLayout — single scroll region (flag-on)", () => {
  it("introduces a sidebar-scroll wrapper", () => {
    expect(source).toMatch(/className="sidebar-scroll"/);
  });
  it("keeps a flag-off path without the wrapper (ternary, not bare &&)", () => {
    // wrapper must be gated on unifiedNav so flag-off is unchanged
    expect(source).toMatch(/unifiedNav\s*\?/);
  });
});
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `ChatLayout.tsx` (chat branch): extract the roster into `const rosterEl = (<div className="agent-list">…existing…</div>);`. Then render: `unifiedNav ? (<div className="sidebar-scroll">{rosterEl}<CompanyNav …/><GallerySection/></div>) : (<>{rosterEl}<GallerySection/></>)`. `new-agent-btn`, header, and footer stay OUTSIDE (pinned).
  - `ChatLayout.css`: add `.sidebar-scroll { flex: 1; min-height: 0; overflow-y: auto; }`. Change `.agent-list` from `{ flex: 1; overflow-y: auto; padding: 0 12px; }` to `{ overflow: visible; padding: 0 12px; }` — flag-OFF still has `.agent-list` as a direct flex child, but with the wrapper gone flag-OFF it must keep its own scroll. To keep flag-OFF identical, scope the override: add `.sidebar-scroll > .agent-list { flex: none; overflow: visible; }` and leave the base `.agent-list { flex:1; overflow-y:auto; padding:0 12px }` unchanged. (This way flag-OFF `.agent-list` is byte-identical; only the flag-ON wrapped one changes.)
  - `CompanyNav.tsx`: change `className="company-nav px-2 py-1 min-h-0 overflow-y-auto"` → `className="company-nav px-2 py-1"` (it now lives inside `.sidebar-scroll`).
- [ ] **Step 4: Run test, expect PASS.** `npx tsc --noEmit`, `npm run lint`.
- [ ] **Step 5: Commit** — `git commit -am "feat(chat): one-scroll sidebar restores the agents roster (flag-on) — Phase D"`

---

### Task 3: Keep the Settings nav visible on the `/settings/connections` route

**Files:**
- Read first: `apps/frontend/src/app/settings/page.tsx` (the `NAV_SECTIONS` render + active-panel logic) and `apps/frontend/src/app/settings/connections/page.tsx` and `apps/frontend/src/app/settings/layout.tsx`.
- Modify: extract the consolidated nav into a shared component and render it on the connections route (see approach).
- Test: `apps/frontend/src/app/settings/__tests__/connections-keeps-nav.test.tsx`

**Bug:** Connections is an `href` to `/settings/connections` (separate route). That page renders WITHOUT the 6-group nav, so clicking it makes every other tab disappear. It must keep the same nav shell (with Connections highlighted), while staying a real route for the OAuth redirect.

**Approach (DRY):** Extract the nav + content-frame into a shared client component `SettingsShell` (renders the `NAV_SECTIONS` sidebar + a `{children}`/active-panel content area), used by BOTH `settings/page.tsx` (active = `?panel`) and `settings/connections/page.tsx` (active = `connections`). The shared `unifiedNav` gating stays (flag-OFF shows the legacy nav). Verify Connections is highlighted when on `/settings/connections`.

- [ ] **Step 1:** Read the three files above; identify the current nav render block in `page.tsx`.
- [ ] **Step 2: Failing test** — assert that rendering the connections route (flag-ON) shows the 6-group nav AND highlights `connections`, and that the other nav rows are still present (not gone).
- [ ] **Step 3: Implement** the `SettingsShell` extraction; render it on both pages. Keep `unifiedNav` gating intact (flag-OFF legacy nav unchanged on both).
- [ ] **Step 4:** Test PASS, `tsc`, `lint`.
- [ ] **Step 5: Commit** — `git commit -am "fix(settings): keep the consolidated nav on the /settings/connections route — Phase D"`

---

### Task 4: Settings nav spacing + MCP/Connections visual consistency

**Files:**
- Read first: the Settings nav markup + its CSS/Tailwind (in `settings/page.tsx` / `SettingsShell` from Task 3) and the `IntegrationsPanel` (MCP servers) + `ConnectionsList` (the connections content).
- Modify: the nav group-label + item spacing; and the content-frame so MCP servers and Connections render with the same padding/width as the other panels.
- Test: `apps/frontend/src/app/settings/__tests__/nav-spacing.test.tsx` (assert the group-label and item classes/structure are consistent across groups)

**Bug:** group-to-item spacing looks off, and MCP servers + Connections look like a different page (different padding/width) from the other panels.

- [ ] **Step 1:** Read the nav markup + the two panels; note the spacing/padding inconsistency.
- [ ] **Step 2: Failing/structural test** for the spacing/class consistency you'll enforce.
- [ ] **Step 3: Implement** — normalize the group-label margin + item gap to one scale; wrap MCP servers + Connections content in the same content-frame/padding as the other panels.
- [ ] **Step 4:** Test PASS, `tsc`, `lint`.
- [ ] **Step 5: Commit** — `git commit -am "fix(settings): consistent nav spacing + MCP/Connections content frame — Phase D"`

---

## Verification (controller, after all tasks)
Push the branch, open a PR (frontend CI now on hosted runners), merge when green, then dogfood on `dev.isol8.co`: flag-ON `/chat` shows the agents roster + no SYSTEM section; flag-ON `/settings` nav has even spacing, Connections keeps the nav, MCP/Connections match the other panels; flag-OFF `/chat` and `/settings` are byte-identical to today.
