# Canvas + the Apps skill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Canvas, a fifth place on the workspace rail that lists the workspace's apps, opens one in the main pane, and lets the owner share, reach the database provider's console and remove; the workspace builder's skill and reviewer; and suggested ideas that start an app.

**Architecture:** Canvas is one new `Place` and one new `PanelKind` in the existing workspace controller and panel registry, a list-plus-detail panel copied from the Ideas panel, and one data hook over plan 1's member routes. The app renders in an `<iframe>` whose `src` is the pass URL from `POST /apps/{id}/open`; the app and the workspace are same-site, so the app's cookie works inside the frame. The skill is one `references/apps.md` plus one routing row.

**Tech Stack:** Next.js 16 / React, lucide-react, SWR via `@lightbulb/shared`'s `createApiClient`, Vitest 3 + Testing Library; Hermes skills built by `apps/infra/hermes/build-skills.sh`.

**Spec:** `docs/superpowers/specs/2026-10-28-apps-design.md` (third draft, approved 29 Oct; §2, §3, §5 bind this plan). Backend API: plan 1 (`2026-10-29-apps-1-backend.md`) "API contract"; CLI: plan 2. Research: `2026-10-29-app-triggers-and-origins.md`, `2026-10-29-builder-harness-engineering.md`. Frontend interfaces digest: session scratchpad `frontend-digest.md`. Line numbers on `origin/main` c825a2f0a; re-check.

## Global Constraints

- The rail reads `Chat, Activity, Work, Library, Canvas`, in that order, and nothing else; Canvas is disabled until `chatReady` like the others (`WorkspaceHeader.tsx:56`).
- Everyone sees every app listed; an app the viewer cannot open is greyed with "ask in #<room>" (spec §4).
- Opening always goes through `POST /apps/{id}/open`; the frontend never builds the app URL itself and never sees a pass longer than it takes to set `iframe.src`.
- The app opens inside the pane with the sidebar still there, plus an "Open in its own tab" control (spec §4).
- Only org admins change sharing (`open_to`: room, workspace, invited, public), invite outside emails and remove an app (`useIsWorkspaceAdmin`). Remove requires typing the app's name. Lightbulb takes no database backups (founder ruling); Canvas links to Neon's own restore instead.
- The room never sees the CLI's words: the skill speaks only preview, live, publish, undo, Canvas (spec §2).
- No new card kind in the room; the builder posts one line per event (spec §8).
- Skill detail lives in `references/apps.md`, never inline in `SKILL.md`; the reference points at `software-projects.md` and `money.md` instead of repeating them.

## File structure

- Modify `src/components/workspace/workspaceController.ts:23,73-78` (`Place`, `placeOf`), `WorkspaceHeader.tsx:16-25` (destination), `panelRegistry.ts:8,286-290,386` (kind, `describePanel("canvas")`, durable `app` param), `panelComponents.ts:15-31` (map).
- Create `src/hooks/useApps.ts`, `src/components/workspace/panels/CanvasDockPanel.tsx`, `src/components/canvas/AppList.tsx`, `src/components/canvas/AppView.tsx`, `src/components/canvas/canvas.css`, tests beside each.
- Modify `next.config.ts:96` (`frame-src`).
- Create `src/components/canvas/AppSettings.tsx` (sharing, invites, budget, data link, remove).
- Create `apps/infra/hermes/skills-src/lightbulb/references/apps.md` and `references/apps-reviewer.md`; modify `SKILL.md:45` (rows), `MAINTENANCE.md` (anchor).
- Modify `apps/backend/core/services/ideas.py` (`kind: "app"` on suggested ideas) and the Ideas "Let's do it" path to start the builder.

## Review Focus

1. A member whose pass request fails (left the room between list and click): the pane shows the refusal text and the list row goes grey, no blank iframe (Task 4 test `shows refusal and greys the row when open is forbidden`).
2. Switching from one app to another in the pane: the old iframe is unmounted before the new `src` is set, so no app ever sees another app's pass (Task 4 test `remounts the frame per app`).
3. An app still `building`: listed with "building", not openable, no spinner that never ends; refresh picks up `ready` (Task 3 test `building apps are listed but not openable`).
4. A phone-width viewport: the list is one column and the frame fills the pane under the rail (Task 3/4: CSS at 390 px; manual check in the acceptance run).
5. The builder's turn ending on `awaiting_approval` and resuming on the room line: the reference must say "end the turn" explicitly, or the agent polls (Task 6 copy check).

---

### Task 1: Canvas as a place and a panel kind

**Files:**
- Modify: `src/components/workspace/workspaceController.ts:23` (`Place` += `"canvas"`), `:73-78` (`placeOf`: `if (main.component === "canvas") return "canvas";` before the teams checks), `src/components/workspace/WorkspaceHeader.tsx:5,16-25` (import `LayoutGrid`; entry `{ place: "canvas", panel: "canvas", label: "Canvas", Icon: LayoutGrid }` last), `src/components/workspace/panelRegistry.ts:8` (`PanelKind` += `"canvas"`), `:286-290` (copy the activity block: `if (key === "canvas") return { panelId: "canvas", component: "canvas", title: "Canvas", params: p }`), `:386` (`durableParamsFor("canvas") → ["app"]`), `src/components/workspace/panelComponents.ts:15-31` (`canvas: CanvasDockPanel`, a placeholder component until Task 3)
- Test: `src/components/workspace/__tests__/WorkspaceHeader.test.tsx:48-56`, `__tests__/panelRegistry.test.ts`, `__tests__/workspaceController.test.ts`

**Interfaces:**
- Produces: `openPanel("canvas", { app?: string }, { pushUrl: true })` opens Canvas; `placeOf` reports `"canvas"`; URL `?panel=canvas&app=<id>` restores it (the reconciler at `WorkspaceShell.tsx:182-220` already routes any registered key).

- [ ] **Step 1: Update the failing tests:** the rail test now expects `["Chat","Activity","Work","Library","Canvas"]`; add `describePanel("canvas", {app: "app_1"})` → `{panelId: "canvas", component: "canvas", title: "Canvas", params: {app: "app_1"}}`; `placeOf({component: "canvas", …}) === "canvas"`.
- [ ] **Step 2:** `cd apps/frontend && npx vitest run src/components/workspace` → FAIL.
- [ ] **Step 3:** Implement the five edits.
- [ ] **Step 4:** → PASS. **Step 5:** Commit `"workspace: Canvas place and panel"`.

### Task 2: The data hook

**Files:**
- Create: `src/hooks/useApps.ts`, `src/hooks/__tests__/useApps.test.tsx`

**Interfaces:**
- Consumes: plan 1 member routes; `useApi()` (`src/lib/api.ts:39-51`), `useIsWorkspaceAdmin()`.
- Produces: `export type AppRow = { app_id; name; room_id; room_name?; state; open_to; budget_usd; preview_url; live_url; live_version; last_opened_at; spend_month_cents; created_by_name?; agent_name?; can_open: boolean }`; `export function useApps(): { apps: AppRow[]; loading: boolean; error: Error | null; refresh(): void; open(appId: string, target?: "live" | "preview"): Promise<string>; update(appId: string, patch: { open_to?: AppRow["open_to"]; budget_usd?: number }): Promise<AppRow> }` (SWR key `"/apps"`, refresh every 15 s while any app is `building`; `open` → `POST /apps/{id}/open` returning `url`; `update` → `PATCH /apps/{id}`; errors through `notifyApiError` like `useIdeas.ts:11-30`).

- [ ] **Step 1: Tests:** `lists apps from /apps`, `open posts and returns the url`, `update patches and refreshes`, `polls while an app is building`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"canvas: useApps hook"`.

### Task 3: The list

**Files:**
- Create: `src/components/canvas/AppList.tsx`, `src/components/canvas/canvas.css`, `src/components/canvas/__tests__/AppList.test.tsx`
- Create: `src/components/workspace/panels/CanvasDockPanel.tsx` (replace the Task 1 placeholder; pattern `IdeasDockPanel.tsx:16-53`)

**Interfaces:**
- Consumes: `useApps`, `PanelProps`, `updatePanelParams`.
- Produces: `export function CanvasDockPanel({ id, params }: PanelProps)`: `params.app` selects the detail (Task 4); `close()` = `updatePanelParams(id, { app: undefined })`. `export function AppList({ apps, onOpen }: { apps: AppRow[]; onOpen(appId: string): void })`: `<section aria-labelledby="canvas-heading">` + `<ul className="app-list">`; each row shows name, one line (`state === "building"` → "building"), "asked by {created_by_name} in #{room_name}", who can open (`room` → "#room", `workspace` → "everyone here", `invited` → "people you invited", `public` → "anyone with the link"), last opened, `spend_month_cents` as dollars; a row with `can_open === false` has `aria-disabled` and the text "ask in #{room_name}"; `state === "building"` rows are not clickable. Empty: `<p className="canvas__notice">No apps yet. Ask an agent in a room to build one.</p>`. Error: `<p role="alert">` + Retry.

- [ ] **Step 1: Tests:** `renders one row per app with who can open it`, `greys an app the viewer cannot open and names its room`, `building apps are listed but not openable`, `empty state copy`, `retry calls refresh`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"canvas: app list"`.

### Task 4: The app in the pane

**Files:**
- Create: `src/components/canvas/AppView.tsx`, `src/components/canvas/__tests__/AppView.test.tsx`
- Modify: `src/components/workspace/panels/CanvasDockPanel.tsx` (detail branch), `next.config.ts:96` (`frame-src` += `https://*.apps.thelightbulb.company`; read the host from `process.env.NEXT_PUBLIC_APPS_DOMAIN ?? "apps.thelightbulb.company"`)

**Interfaces:**
- Consumes: `useApps().open/update`, `useIsWorkspaceAdmin()`.
- Produces: `export function AppView({ app, onBack }: { app: AppRow; onBack(): void })`: header with Back, name, `v{live_version}`, "Open in its own tab" (`window.open(url, "_blank", "noopener")` after a fresh `open()`), and for admins a Settings button opening `AppSettings` (Task 4b); body: on mount calls `open(app.app_id)` and renders `<iframe key={app.app_id} title={app.name} src={url} sandbox="allow-scripts allow-same-origin allow-forms allow-popups" className="canvas__frame" />`; while waiting, "Opening…"; on refusal, `<p role="alert">` with the backend's message and no iframe. The `key` forces a remount per app.

- [ ] **Step 1: Tests:** `requests a pass and sets it as the frame src`, `shows refusal and greys the row when open is forbidden`, `remounts the frame per app` (two apps in sequence → two distinct iframes), `admins see the Settings button, members do not`, `open in own tab requests a fresh pass`.
- [ ] **Steps 2–5:** FAIL → implement → PASS (also `npx vitest run src/components/workspace`) → commit `"canvas: app view in the pane"`.

### Task 4b: App settings (sharing, invites, budget, data, remove)

**Files:** Create `src/components/canvas/AppSettings.tsx`, `src/components/canvas/__tests__/AppSettings.test.tsx`; extend `src/hooks/useApps.ts` with `invite(appId, emails)`, `databaseLink(appId)`, `remove(appId, confirmName)`.

**Interfaces:**
- `AppSettings({ app, onClose })`: a radio group for who can open it, worded for people ("This room", "Everyone in the workspace", "People I invite", "Anyone with the link"), an email list with Invite when "People I invite" is chosen, a monthly budget field, "Data and backups" (opens Neon's own console for the live database through the rail's existing `POST /company/provisioning/resources/{provider}/{resource_id}/dashboard-link`, where restores happen), and "Remove app…" (type the name). Calls plan 1's `PATCH /apps/{id}`, `POST /invites`, `DELETE /apps/{id}` and the dashboard-link route.

- [ ] **Step 1: Tests:** `choosing people I invite shows the email list`, `data and backups opens the provider console link`, `remove needs the typed name`, `members never see the settings`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"canvas: app settings"`.

### Task 5: Canvas deep link from a room line

**Files:**
- Modify: `src/components/workspace/WorkspaceShell.tsx:182-220` only if `?panel=canvas&app=` does not already restore the detail after Task 1 (write the test first; implement only on failure)
- Test: `src/components/workspace/__tests__/WorkspaceShell.test.tsx` (add)

- [ ] **Step 1: Test:** `opens Canvas on app_1 from ?panel=canvas&app=app_1`.
- [ ] **Step 2:** Run; if PASS, skip to Step 5. **Step 3:** Implement the smallest change. **Step 4:** PASS. **Step 5:** Commit `"canvas: deep link"`.

### Task 6: The skill

**Files:**
- Create: `apps/infra/hermes/skills-src/lightbulb/references/apps.md`
- Modify: `apps/infra/hermes/skills-src/lightbulb/SKILL.md:45` (add the row after `software-projects.md`), `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (anchor "Apps")
- Test: `apps/infra/hermes/build-skills.sh` runs clean; the existing skills-src tests (find them with `git grep -l skills-src apps/infra/hermes/*test*`) pass

**Interfaces:**
- Produces `references/apps.md`, short sections in this order:
  1. **When this applies:** anyone asks for software of any kind (site, tool, form, dashboard, portal, product, automation), says "Let's do it" on a suggested app idea, or an outside request needs one. Nothing is declined. Copying an app or site, look included, is fine.
  2. **Start:** read the thread, the workspace's name, logo and brand, and any URL, screenshot or file; pick the kind (spec §4); `lightbulb app new <name> --kind <kind> --room $ROOM_ID` in `$HERMES_HOME/.run/<name>`; follow the app's `AGENTS.md` and `.lightbulb/plan.md`. Ask at most one card (`references/ui.md`), only what you can't read; every option skippable. For customer-facing apps and "like X" requests, offer the one optional chip "Look at what others in your market do first?" and, if taken, research in parallel and return one card of 3–5 comparable products.
  3. **The loop:** `lightbulb app check` → fix → `lightbulb app up --json`. Follow `next` exactly: on `diagnose`, stop patching and find the root cause; on `undo`, undo and say plainly what failed.
  4. **Waiting for approval:** if `state` is `awaiting_approval`, say in one line that there's a card to approve, then **end the turn**; the approval posts a line in this room that wakes you. Never poll.
  5. **Done means proven:** when `up` returns a preview, open it in the shared browser, walk the main flow, record it (`lightbulb app evidence --kind browser --file …`), and run `up` again. Post only when it says `done: true`: one line with the Canvas link, what the app does, what data it reads, and what it cost. If `sample_data` is true, say real data arrives in a few minutes.
  6. **Changes and publishing:** edits → the same loop, one line per change. "ship it" → ask the reviewer (`references/apps-reviewer.md`) for a review pass, then `lightbulb app publish`. One line when it's live.
  7. **Undo and data:** "undo" → `lightbulb app rollback`; say which version is live and that data is unchanged. Data restores happen in the database provider's own console, linked from Canvas; Lightbulb takes no backups.
  8. **Words:** in the room say only preview, live, publish, undo, Canvas. Never environment, staging, production, branch, migration, deploy, database, Vercel, Neon.
  9. **Rules:** never a secret in code or the thread (secrets go through the form); never remove the door; the schema only grows (a destructive change needs the owner's card, then `--approved <id>`); write a lesson after every resolved failure; report a broken tool once with `lightbulb app report-broken-tool`; if the workspace has GitHub connected (`gh auth status`), push the app's repo to the company's GitHub after each publish; keep maintaining the app in the room it was built in.
- Produces `references/apps-reviewer.md`: the reviewer is a separate agent run (Paperclip task assigned to a second roster agent, or a subagent with fresh context) given only the preview URL, the app's purpose and `AGENTS.md`; it walks every flow in the shared browser, probes as two different users, checks phone width, and records `lightbulb app evidence --kind review` with a pass or a list of failures. It never edits code.
- Routing rows for `SKILL.md`: `references/apps.md` (any request for software, a suggested app idea, or an outside request), `references/apps-reviewer.md` (reviewing an app before it goes live).

- [ ] **Step 1:** Write `apps.md` (under 120 lines), the row, the anchor.
- [ ] **Step 2:** `bash apps/infra/hermes/build-skills.sh` → clean; skills tests pass.
- [ ] **Step 3:** Read it back as the builder: every command exists in plan 2, every state and field (`done`, `sample_data`, `next`, `cost_cents`) exists in plans 1–2, and no forbidden word appears in a room-facing sentence.
- [ ] **Step 4:** Commit `"skill: apps reference"`.

### Task 6b: Suggested ideas that start an app

**Files:** Modify `apps/backend/core/services/ideas.py` (`suggest_idea` accepts `kind: "app"` with `app_kind` from spec §4 and a one-line `origin`), the Ideas "Let's do it" handler (an app idea starts the workspace builder in the idea's room with the idea text as the request and `origin: {kind: "suggested", ref: idea_id}`), `references/ideas.md` (when to suggest an app: a sheet outgrown, a chore or question repeating, a SaaS with unused seats); tests in `tests/unit/services/test_ideas.py`.

- [ ] **Step 1: Tests:** `app_idea_round_trips_kind_and_origin`, `lets_do_it_on_an_app_idea_starts_the_builder_in_its_room`, `existing_ideas_unchanged`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"ideas: suggested apps"`.

### Task 7: Acceptance

- [ ] **Step 1:** With plan 1 deployed and plan 2's CLI in the workspace image, run spec §3 on Lightbulb Demo Lab: ask in a room, approve, open the first (sample-data) preview from Canvas on a laptop and a phone browser, see it switch to real data, publish after the reviewer's pass, open it live, undo, open "Data and backups", invite an outside email, make it public.
- [ ] **Step 2:** Measure Koomen's number: minutes from "build me" to a second person opening it in Canvas with no account created; record it in `docs/research/2026-10-29-apps-spike-revealed-keys.md`.
- [ ] **Step 3:** Open the PR with the pr skill. **Do not merge without the founder.**
