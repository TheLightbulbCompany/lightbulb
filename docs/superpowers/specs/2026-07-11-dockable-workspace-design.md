# Dockable Workspace for the /chat Shell — Design

**Date:** 2026-07-11
**Status:** Approved (brainstorm complete; pending implementation plan)
**Library:** dockview-react v7 (pinned; v7.0.2 at time of writing — MIT, zero-dep, React 19 peers)

## 1. Intent

Rebuild the `/chat` desktop shell so its surfaces — agent chats, Teams panels
(inbox, tasks, dashboard, …), workspace file viewer, agent inspector — live in a
LeetCode/VS Code-style dockable layout: drag a panel by its tab, edge drop =
split, center drop = tab stack, resizable gutters, layout persists per user,
"Reset layout" always available. Everything is free-form editable; presets
don't constrain anything (and v1 ships only the default layout — no preset
menu yet).

## 2. Decisions (resolved with the user)

| Decision | Resolution |
|---|---|
| V1 dockable surfaces | Chat, file viewer, agent inspector, the 8 nav-visible Teams list panels (dashboard, inbox, issues/tasks, routines, goals, projects, org-chart, activity) and the 4 Teams detail panels (issue-, routine-, project-, goal-detail). Sidebar + header stay fixed chrome. The seven /settings-homed control panels, sessions/overview, and the nav-dark teams-agents/settings/costs stay out. |
| Multi-instance | One chat panel **per agent** (opening an already-docked agent focuses it). Everything else singleton, except Teams detail panels which are per-`detailId` (like files in an editor). No same-agent multi-session panels in v1. |
| Persistence | localStorage only. Version-stamped blob, owner-scoped key. No backend prefs endpoint (none exists; frontend-only project). |
| URL model | **One-way command channel** (see §6). URL = last navigation intent, never a focus mirror. Focus lives in the layout blob. |
| Default layout | **Mission Control** (Friday chat + Tasks + Inbox) — a deliberate break from "default mirrors today"; Reset re-applies it. Classic single-chat arrangement is reachable by closing panels; no preset menu in v1. |
| Rollout | Direct replacement of the desktop shell, no feature flag, no canary (single active user). Rollback = revert the PR. `ChatLayout` survives **only as the mobile (<768px) shell**. |
| Sequencing | Builds on shipped #1216/#1217 (refcounted `teamsSubscription`, `RunNotifications`, notify extensions) and #1213 (team billing renders inside the provisioning gate). |

## 3. Architecture

Desktop (≥768px, JS `matchMedia`; crossing the breakpoint remounts the other
shell — chat state survives via module stores + the layout blob):

```
chat/page.tsx (Suspense)
└─ MarketplaceDrawerProvider
   └─ GatewayProvider                      ← ONE (single WS). Unchanged.
      ├─ ExecApprovalsProvider             ← HOISTED out of ChatLayout: useAgentChat
      │  │                                    degrades silently without it.
      │  └─ WorkspaceShell
      │     ├─ Sidebar     (fixed chrome, extracted from ChatLayout)
      │     ├─ HeaderBar   (fixed chrome: hamburger, file button, support, UserButton;
      │     │               per-agent actions move INTO chat panels)
      │     └─ Provisioning/onboarding gate (wraps the ENTIRE dock zone)
      │        └─ TeamsProvidersHost       ← single mount of InlineTeamsNavProvider +
      │           │                           TeamsEventsProvider + CommandPaletteProvider,
      │           │                           rendered only while ≥1 teams panel is in the layout
      │           └─ DockviewReact         (client-only via next/dynamic ssr:false;
      │                                     dockview CSS import + v7 theme OBJECT prop;
      │                                     disableFloatingGroups; no popout groups)
      ├─ MarketplaceDrawer / MarketplaceToast / RunNotifications   (siblings, as today)
      └─ ShellServices (renderless): user-sync latch, Stripe checkout-return polling
         (+ ?checkout/?subscription/?credits strips), marketplace badge fetch,
         GetStarted pipeline + floating guide, Trial/OutOfCredits banners
```

Load-bearing invariants:

- **The dock mounts only at container phase `ready`.** All provision-gate /
  billing-blocked states (including #1213's `team_billing_required` →
  `TeamBillingCard`) render upstream in the ProvisioningStepper. This is what
  guarantees Teams panels never paint a 402/paywall overlay. Any future
  "render the dock earlier" change must revisit this.
- **Exactly one `GatewayProvider`** — duplicate providers mean duplicate
  sockets and duplicate Tauri auth-push effects. Gateway pub-sub registries
  are Sets; N panels under one provider compose cleanly.
- **TeamsEventsProvider** (single mount) uses the shipped refcounted
  `teamsSubscription` helper; the acquire-effect-before-resubscribe-effect
  ordering contract applies. One `CommandPaletteProvider` → one Cmd+K palette.
- `InlineTeamsNavProvider.openDetail` is implemented by the shell's
  `openPanel()` (see §6) — today it defaults to a no-op the shell must fill.
- `useTeamsWorkspaceStatus` fires `/teams/dashboard` (3s × 30 retry on 202)
  the moment any Teams panel mounts — gating TeamsProvidersHost on layout
  content keeps teams-free layouts from paying it.
- **RunNotifications**: exactly one mount inside GatewayProvider. Its
  suppression context ("is the user watching") is recomputed from dockview
  state: a chat run is suppressed iff that agent's chat panel is the active
  tab of a visible group; a teams run iff any teams panel is visible.

## 4. Panels

Registry: `panelKey → { component, title, instancing, paramsSchema, loader, renderer }`.
Deterministic dockview panel IDs implement instancing (`addPanel` throws on
duplicates; open = `getPanel(id) ?? addPanel(...)`): chat = `chat:<agentId>`,
singletons = the key, details = `<key>:<detailId>`.

| Panel | Instancing | Params | Adaptation |
|---|---|---|---|
| Chat | per-agent | `agentId` | New ChatPanel wrapper around AgentChatWindow (already accepts `agentId: string \| null`). `renderer: 'always'` — the dockview default (`onlyWhenVisible`) detaches hidden DOM and loses native scroll position, which MessageList's `isNearBottomRef` model cannot recover from. |
| File viewer | singleton | `path?` | Move inline `<style>` block (FileViewer.tsx:153-187) into a CSS file; sever `grid-area: viewer` coupling; scope FileContentViewer's window Cmd+S to the focused panel. |
| Agent inspector | singleton | `agentId`, `initialTab` | Params replace `?agent`/`?inspectorTab` reads. |
| Teams lists ×8 | singleton | filters (tasks: `agent`, `new`) | Keep `next/dynamic` code-splitting + idle preload. Migrate off `useSearchParams`: IssuesPanel (`?agent`, `?new`), InboxPage (`?newIssue`; **delete** its `?tab` URL sync — localStorage persistence already exists), LLMPanel (`?action`). Scope inbox j/k keyboard nav to panel focus. |
| Teams details ×4 | per-detailId | `detailId` | Multiple details open as tabs. "Row gone" empty state on 404 (backstop for external deletes); same-tab delete uses the shell close API (§6). |

**Selection-layer surgery** (the core change):

- Sidebar roster click → `openChatPanel(agentId)`: focus existing or open new.
- The `selectAgent` window CustomEvent survives for external initiators
  (MarketplaceToast, RunNotifications clicks, mobile ChatLayout); its desktop
  listener moves into WorkspaceShell and routes to `openChatPanel`. The
  page-level `selectedAgentId` state slot is deleted on desktop.
- Per-agent header cluster (pause/resume/inspector/work + roster read) moves
  into each chat panel.
- `getstarted:focus-composer` targets the focused chat panel's composer only.
- `useActiveSession` survives unchanged (keyed per-agent; one panel per agent).

**Default-agent sentinel.** The code-built Mission Control layout cannot know
Friday's agentId (roster arrives via WS RPC, up to minutes on a cold gateway;
never block layout construction on it). Its chat panel gets
`agentId: "__default__"`, resolved at render via `useAgents().defaultId`; the
**sentinel is what persists** — it durably means "the CEO/default agent."
Open-or-focus unifies identities: opening agent X when a sentinel panel
currently resolves to X focuses the sentinel panel (and vice versa).

**Agent-gone gating.** A chat panel may render its "agent no longer exists"
empty state only once the roster has settled: `data !== undefined` (NOT
`isLoading === false`, which SWR reports during error-retry; NOT roster
membership against the `?? []` default). Sentinel panels are exempt.

**Transcript-loss fix (bundled).** `useAgentChat` unsubscribes on unmount and
skips the history fetch when `_messageCache` has the key — a run completing
while a panel is closed is invisible until full reload. Fix: revalidate
history on mount (cache still paints instantly).

## 5. Layout persistence

- **Key:** `isol8.workspace.layout.v1:{orgId ?? userId}` (Clerk; scope
  finality is guaranteed because the onboarding gate blocks the dock until
  identity + org activation settle — a future refactor must not hoist the
  restore above that gate). Template: `desktopPermissions.ts` (version in key
  AND in-payload `version: 1` validated on read).
- **Value:** `{ version: 1, savedAt, layout: dockview.toJSON() }` — panel
  params ride inside. **One-shot intent params are never serialized** (§6).
- **Writes:** debounced ~500 ms on `onDidLayoutChange`; flushed on `pagehide`,
  `visibilitychange:hidden`, and dock unmount (recovery regression unmounts
  the dock mid-session — verified reachable). Not `beforeunload` (unreliable
  in the Tauri webview). Write failures try/catch-swallowed (repo convention);
  the session becomes non-persistent, never crashes.
- **Restore** (effect-time, `typeof window` guarded): parse/version failure →
  default layout. Then a **sanitizer** runs before `fromJSON`: unknown
  `contentComponent` names are remapped to a registered `missing-panel`
  placeholder (dockview `fromJSON` THROWS and aborts the whole restore on
  unknown components — one stale key must not nuke the layout); also register
  the literal `'unknown'` key (deserializer default); strip
  `floatingGroups`/`popoutGroups`/`edgeGroups`. `fromJSON` itself wrapped in
  try/catch → default layout (v7 cleans up safely on throw).
- **Multi-tab:** last-write-wins, no live cross-tab layout sync. Required fix:
  key-filter the two unfiltered window `storage` listeners
  (`useGateway.tsx:~82`, `DesktopPermissionsGate.tsx:~46`) so cross-tab layout
  writes stop re-rendering the gateway provider; preserve `key === null`
  (localStorage.clear) handling.
- **Reset** re-applies `buildMissionControlLayout()` — a code function, so the
  default evolves with releases. Mission Control: sentinel Friday chat (left,
  ~55%), Tasks board + Inbox stacked (right).
- Tauri desktop = separate WKWebView storage partition = independent per-device
  layout. Fine (window geometry differs anyway); durability there is already
  proven by desktopPermissions.

## 6. URL model — one-way command channel

The layout blob owns layout **and focus**. The URL is only a retained
navigation-intent log. This matches today's production semantics (pushes only
on explicit navigation; agent selection never was URL state) — the rejected
alternative ("URL mirrors focused panel") was adversarially reviewed and
generated an open-ended accretion of sync rules.

**Rules:**

1. **Inbound reconciler** (shell-level `useSearchParams` effect with a
   lastApplied guard): whenever `?panel`/params change and disagree with the
   dock, execute as **open-or-focus**. Never closes anything. This single
   mechanism serves external deep links, Back/forward (popstate is just
   another searchParams change), and every existing raw-History producer
   unmodified — CommandPalette's `pushState`, ~20 `handleShellLinkClick`
   consumer files, GoalsPanel/InboxPage fallbacks, ChatErrorCard. No origin
   filtering or reentrancy machinery: nothing writes the URL from focus events.
2. **Outbound:** only explicit navigation actions write the URL (push),
   funneled through one shell API — `openPanel(key, params, { pushUrl })`.
   Panel **focus clicks write nothing**. Invariant (tested): *panel focus
   produces zero history writes.* This keeps PostHog `$pageview` at today's
   cardinality — no filtering layer needed.
3. **Clear-on-close:** closing the panel the URL currently names does one
   `replaceState` clearing its **full durable param set** (`panel` +
   `detailId`/`agent`/`inspectorTab` per the registry) — kills
   refresh-resurrection. Implemented on `onDidRemovePanel` using the same
   URL→panel-id resolver as the reconciler; suppressed during shell-API
   compound operations.
4. **Load order:** restore blob (layout + focus) → execute retained URL
   command on top (**URL wins focus**; chosen over open-if-missing so a pasted
   link to an already-open panel still visibly focuses it).
5. **One-shot intents** (`?new`, `?newIssue`, `?action`) vs **durable params**
   (`detailId`, `initialTab`, `agent`-as-filter): one-shots are copied to the
   target panel's open-time params, immediately stripped from the URL via
   `replaceState`, cleared from panel params after consumption, and excluded
   by the persistence serializer — otherwise dockview persists the intent and
   the create-dialog re-fires on every reload.
6. **Alias maps** (`credits→llm`, `approvals→inbox`, `members→settings`,
   `skills→dashboard`) move into the inbound resolver, applied replace-only
   (this also fixes the live `credits` Back-trap: ControlPanelRouter's remap
   currently PUSHES, making Back unable to escape the entry). Delete both
   panel-router remap effects.
7. **`/chat?agent=<uuid>`** (no `?panel`) = open-or-focus that agent's chat
   panel — defines the currently-dangling marketplace deploy-success link.
   Unknown agent → focus default chat. Bare `/chat` = no command. `?agent`
   keeps its panel-scoped meanings when `?panel` is present.
8. **Detail-delete shell API:** `closeSubjectDeleted(listPanelKey)` — in
   order: (a) replace the URL with the list panel's URL, (b) close the calling
   detail panel with the clear-on-close listener suppressed (so it can't fire
   a competing replace), (c) open-or-focus the list panel. Replaces the three
   self-navigation sites (IssueDetailPage, GoalDetailPage, AgentDetailPanel)
   that read `window.location.search` directly today.
9. **Grammar parity:** URLs stay byte-identical in meaning across the desktop
   dock and the mobile classic shell — no dockview-only params in shareable
   URLs. `ChatErrorCard` migrates off `next/link` to anchor +
   `handleShellLinkClick` (its router navigation violates the shellNav rail
   and pays an RSC round-trip today). Anchors keep real hrefs so
   modified-clicks open new tabs where the URL executes as a deep link.

**Accepted trade-offs (documented, deliberate):**

- The address bar names the last navigation, not necessarily the focused
  panel. Teams detail links are org-shareable, so a "copy link to this panel"
  affordance (builders already exist in `shellNav.ts`) is a named fast-follow.
- Refresh focuses the URL-named panel even if the user last focus-clicked
  elsewhere.
- Back is open-granular, not focus-granular, and never closes panels — the
  workspace accumulates open panels that history traversal won't remove.

## 7. Error handling & edge cases

- Blob failures: §5 (sanitize → placeholder; try/catch → default; swallow
  writes).
- New org member's first Mission Control paint: provisioning overlay in the
  two Teams panels for ~3–6 s (shared SWR key; both flip together;
  self-resolves via the 202 poll). Accepted.
- Dock unmount mid-session (ready→recovery regression): flush persistence on
  unmount; remount re-runs restore + URL command idempotently.
- Deleted detail rows: "row gone" empty state (backstop) + shell close API
  (same-tab polish).
- Missing-panel placeholder renders an empty state with a close button.
- Breakpoint crossing remounts the other shell; module stores + blob carry
  state.

## 8. Testing

Constraints: single-file vitest runs only (never the full suite);
`tsc --noEmit` after adding tests; new analytics events must join the
`AnalyticsEvent` union or typecheck fails.

- **Unit:** blob serializer/sanitizer (version validation, unknown-key remap,
  one-shot exclusion, floating/popout strip); URL resolver/reconciler (alias
  maps, full param inventory, one-shot strips, `/chat?agent` grammar,
  lastApplied idempotency); clear-on-close param scoping per panel class;
  registry instancing (per-agent IDs, sentinel/concrete unification, singleton
  focus-not-duplicate).
- **Component:** chat sentinel resolution; agent-gone gating on
  `data !== undefined`; missing-panel placeholder.
- **Invariant test:** panel focus produces zero history writes.
- Centralize the Node-22 localStorage test polyfill (currently copy-pasted in
  6+ test files) into a shared helper.
- **E2E:** none in v1 (journey test is on-demand by policy); manual validation
  on dev.

## 9. Rollout & sequencing

Two stacked PRs (worktree branch `feat/dockable-workspace` off origin/main;
no push without approval; Codex reviews):

1. **PR-1 — extraction + hazard fixes, zero behavior change:** ShellServices
   out of ChatLayout; ExecApprovals/Teams-provider hoists prepared; storage
   listener key filters; transcript-loss fix; FileViewer style/Cmd+S fixes;
   ChatErrorCard off next/link; localStorage test-polyfill helper. Classic
   shell untouched in behavior.
2. **PR-2 — the workspace shell:** dockview + registry + persistence +
   reconciler + Mission Control default, replacing the desktop classic path
   outright. `ChatLayout` remains as the mobile shell only.

No feature flag, no canary (single active user). Rollback = revert PR-2.
Analytics: `workspace_layout_reset`, `workspace_panel_opened` added to the
union.

**Fast-follows (explicitly out of v1):** preset menu (the default+Reset blob
mechanism already is the preset mechanism), "copy link to panel", per-agent
run badges on tabs/sidebar (via the sanctioned RunNotifications run-map
extraction), Deep Work/Triage presets, floating/popout panels, same-agent
multi-session panels, mobile dock, backend layout persistence.

## 10. Key verified references

- Shell: `src/app/chat/page.tsx` (providers :171-208; param derivation
  :38-41; nav helpers :50-128), `src/components/chat/ChatLayout.tsx`,
  `ChatLayout.css` (grid :1-30; mobile :517-555), `src/lib/shellNav.ts`.
- Panels: `ControlPanelRouter.tsx` / `TeamsPanelRouter.tsx` (both
  props-driven), `InlineTeamsPanel.tsx` (provider stack + workspace gate).
- Landed dependencies: `src/lib/teamsSubscription.ts`,
  `src/components/notifications/RunNotifications.tsx` + `runToastReducer.ts`
  (#1217); GetStarted dismissal (#1216); `TeamBillingCard` inside
  ProvisioningStepper (#1213).
- Hazards fixed by this work: `useGateway.tsx:~78-87` +
  `DesktopPermissionsGate.tsx:~46` unfiltered storage listeners;
  `FileViewer.tsx:153-187` global style block; `FileContentViewer.tsx:62-71`
  window Cmd+S; `useInboxKeyboardNav.ts:~187` document keys;
  `useAgentChat.ts:727-733` cache short-circuit; ControlPanelRouter
  credits-remap Back-trap; `ChatErrorCard.tsx:130` router Link.

Line refs verified 2026-07-10/11 against origin/main; re-verify during
implementation planning — main moves fast.

## Implementation deviations (PR-2)

Recorded where the shipped PR-2 implementation diverged from the plan above.
Each entry cites where to verify it in the current tree.

1. **Teams providers mount unconditionally above the dock, not gated on
   layout content.** §3 originally called for `TeamsProvidersHost` to render
   `InlineTeamsNavProvider` / `TeamsEventsProvider` / `CommandPaletteProvider`
   "only while ≥1 teams panel is in the layout." As built,
   `WorkspaceShell.tsx` wraps the entire shell (sidebar, header, dock) in all
   three providers unconditionally (`WorkspaceShell.tsx:317-372`). Reason:
   `RunNotifications` — mounted once inside `GatewayProvider`, above
   `WorkspaceShell` — already holds a **permanent** refcount on the teams
   event subscription for its own lifetime via
   `acquireTeamsEvents()`/`releaseTeamsEvents()`
   (`RunNotifications.tsx:178-181`), so gating the providers on layout
   content would not have avoided the socket-subscription cost anyway — it
   was already being paid unconditionally. The actually-expensive piece —
   `useTeamsWorkspaceStatus`'s `/teams/dashboard` poll (3s × 30 retries on
   202) — is instead gated per panel: each `TeamsDockPanel` instance calls
   the hook independently (`TeamsDockPanel.tsx:27`), so the poll only starts
   once a teams panel is actually opened, and SWR dedupes concurrent calls
   across multiple simultaneously-open teams panels into one shared request
   (`useTeamsApi.ts:158-184`, `dedupingInterval: 2000`).
2. **Legacy control panel keys open as dock panels for URL grammar parity,
   despite not being sidebar surfaces.** `panelRegistry.ts`'s `CONTROL_KEYS`
   map (`overview`, `sessions`, `channels`, `usage`, `agents`, `skills`,
   `llm`, `integrations`, `dreams`, `skill-proposals`) resolves each key to a
   `component: "control"` dock panel purely so existing internal links
   (`ChatErrorCard` → `?panel=llm`, `AgentManageSection` →
   `?panel=agents&agent=…`) keep working under the byte-identical-URL-grammar
   rule (§6 rule 9) — see the comment at `panelRegistry.ts:59-61`. None of
   these keys are reachable from the workspace sidebar; `/settings` remains
   their canonical UI home.
3. **`RunNotifications` suppression keys differ by shell.** The desktop
   shell's `watchedChatKeys` set contains both a bare per-agent key
   (`agentId`) for every visible chat panel *and* a compound
   `agentId:sessionId` key for whichever session that panel is currently
   showing (`buildDesktopWatchedKeys`, `RunNotifications.tsx:61-68,167`) —
   needed because a dock can show several chat panels at once, and a run in
   a panel whose active session isn't the one that started it must still be
   suppressed. Mobile builds **compound-only** keys
   (`RunNotifications.tsx:155-160`) — no bare-agent key — to stay
   byte-equivalent with the pre-dock single-chat suppression logic, where
   only one chat view is ever active at a time.
