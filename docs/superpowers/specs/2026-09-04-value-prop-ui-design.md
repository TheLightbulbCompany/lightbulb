# Value-prop UI changes — design

Date: 2026-09-04
Status: approved, not yet implemented
Branch: `feat/value-prop-ui-2026-09-04`

## Why

The product does not communicate its value proposition on first contact. Four
changes, agreed with the owner:

1. Retire the dynamic dockable layout — tabs only.
2. Friday is the visible agent; the rest of the roster collapses behind an
   expander, with pinning to keep chosen agents visible.
3. Board view becomes the default on Tasks.
4. Landing copy: new headline, and re-pitch the launch pill.

Changes 1–3 are one product bet: **Friday is the product, the team is behind
her.** Change 4 sells that bet on the landing page.

Verified against `main` @ `fee9a35cf`. Every claim below carries a citation;
prior exploration done on a 111-commit-stale branch was discarded.

---

## §1 — Tabs only

### What is actually there today

The shell is **dockview-react 7.0.2** (`apps/frontend/package.json:31`), a
recursive split/dock tree of groups where **each group is already a tab
stack**. The tab strip is deliberately themed — 36px rail, 2px active
underline (`workspace.css:179-243`).

Tabs are not being added. Splitting is being removed.

Two things create splits, and only two:

- `buildMissionControlLayout` (`buildDefaultLayout.ts:14-41`) — the default
  layout adds three panels in three groups: Chat left, Browser right, Inbox
  below Browser.
- User drags.

Every *programmatic* open already behaves as "add a tab to the current group":
`addOrFocus` calls `addPanel` with no `position` (`workspaceController.ts:201-207`),
and dockview falls back to the active group (`dockviewComponent.js:1993`).

### Decisions

**D1.1 — Keep dockview.** Ripping it out would mean rebuilding the tab strip,
keyboard tab navigation, the panel registry, the URL reconciler, persistence
and pruning. All of that is orthogonal to "tabs only" and all of it works.
~2,136 LOC of docking machinery stays; the change is to how it is configured.

**D1.2 — `disableDnd: true`** in the dockview options
(`WorkspaceShell.tsx:405-410`). `resolveDndCapabilities` returns
`{ html5: false, pointer: false, pointerHandlesMouse: false }`
(`dndCapabilities.js:4-7`), which disables every drag entry point at the
source: tab reorder, cross-group drag, dock-edge drop, and the group-void
drag handle. This is one option rather than four handler removals, and it
cannot drift out of sync with a future dockview upgrade the way
handler-by-handler suppression would.

Already off and staying off: floating groups (`disableFloatingGroups`),
popout windows (never registered), maximize (no UI trigger exists in 7.0.2),
tab context menus (`getTabContextMenuItems` omitted).

**D1.3 — Single-panel default layout.** `buildMissionControlLayout` drops to
one `addPanel` for Chat. Browser and Inbox open on demand as tabs, which is
already how every other panel opens.

**D1.4 — Bump the persisted layout key `v2` → `v3`**
(`layoutStorage.ts:18`, version check at `:341`). Without this every existing
user restores their saved three-pane split and never sees the change. The file
already performs exactly this migration for v1 (deleted on read, `:19`, `:337`),
so the pattern is established. A `null` read falls through to the default
layout (`WorkspaceShell.tsx:232-242`).

This discards saved layouts. That is the intent, and with splitting gone
there is little left to discard.

### Consequences to accept

- **Browser stops being side-by-side with chat.** The live browser view was
  the strongest argument for the split layout. It becomes a tab.
- **`useBrowserToolAutoOpen`** (`useBrowserToolAutoOpen.ts:44-56`) will now
  pull focus to a tab mid-run rather than updating an already-visible pane.
  Needs a decision at implementation time: keep the auto-open (focus steal) or
  demote it to a passive tab-added state. Recommend the latter.
- Sashes disappear with no code change — one group has no splits to resize.

### Untouched

Tab strip, keyboard tab navigation (roving focus, Delete/Backspace close —
`tabs.js:349-386`), the URL reconciler, deep links, `DEFAULT_AGENT_SENTINEL`
unification, layout pruning, "Reset workspace layout"
(`WorkspaceHeader.tsx:58-69`).

---

## §2 — Friday first; collapsible roster; pinning

### The decision being reversed

`AgentRoster.tsx:24-27` states:

> `// Deliberately NOT here: folders / collapsible groups / manual drag-order.`
> `// Plans cap an owner at 10 agents, so grouping hides four rows behind a`
> `// chevron and manual ordering asks the user to maintain what the sort`
> `// already derives.`

That argument is about **screen space**: all ten rows fit, so hiding some buys
no visibility. The new reason is **focus** — Friday must read as the product,
not as row one of ten. The space argument is not wrong; it is answering a
different question.

**This comment must be rewritten as part of the change**, not left standing.
A stale rationale that contradicts the shipped behaviour is worse than no
comment.

### Decisions

**D2.1 — One mechanism, not two.** "Dropdown for the other agents" and
"expand/hide the agent list" are the same control: a collapsible roster
section. There is no separate combobox. Friday and pinned agents are always
visible; everything else lives behind the expander.

**D2.2 — Pin state is a prop, never a lookup inside the sort.**
`orderRoster` is documented pure and total, with a recorded shipped bug from
reading localStorage inside a `useMemo` during render
(`AgentRoster.tsx:161-165`). So:

- `orderRoster(entries, pinnedIds)` takes pins as an argument.
- Sort order becomes: CEO first, then pinned, then the existing attention rank
  (`STATE_RANK`), then name.
- The hosts (`WorkspaceSidebar`, `ChatLayout`) read and write pin state and
  pass it down. The roster component itself touches no storage.

**D2.3 — Persist pins via the existing per-company helper.**
`components/teams/shared/lib/inboxStorage.ts` (`inboxStorageKey()` →
`paperclip:inbox:<companyId>:<key>`), already used by `InboxPage.tsx:125-140`
for the last-selected tab. No new storage concept, no backend change.

Skipped: cross-device sync via a server-side user setting. Add it when someone
asks; a pin is a per-machine convenience.

**D2.4 — Indicators survive collapse for free.** The section label already
renders `N needs you` / `N working` / count (`AgentRoster.tsx:281-292`), so a
collapsed roster still reports that a hidden agent needs attention. The
expander inherits this; nothing new to build.

Everything else stays: `RosterState` vocabulary, `LiveRunDot`, the CEO badge,
published-agent gold pip, the hover order-freeze (`:233-263`), per-row
actions, keyboard reachability.

**D2.5 — Chat becomes a singleton panel.** Today chat is multi-instance,
`chat:<agentId>` (`panelRegistry.ts:123-125`), which is what produces N chat
tabs. It becomes a singleton `"chat"` with `agentId` as a param — the exact
shape `file-viewer` already has in the same file (`panelRegistry.ts:145-153`),
where re-opening retargets via `updateParameters` (`workspaceController.ts:181-199`).

This is the change with the widest reach. It touches:
- `openChatPanel` and the four-way sentinel unification
  (`workspaceController.ts:232-257`)
- the snapshot resolver `agentIdOfChatPanel` (`:88-93`)
- dead-agent pruning (`layoutStorage.ts:236-246`) — which becomes mostly moot
- `clearClosedPanelFromUrl` sentinel/concrete twin handling (`urlChannel.ts:110-129`)
- `isCommandTargetOpen` (`workspaceShellReconciler.ts:91-105`)
- `RunNotifications`' `visibleChatAgentIds` suppression
  (`RunNotifications.tsx:175,197-212`) — with one chat panel, "is this agent
  visible" collapses to "is this the selected agent"

`DEFAULT_AGENT_SENTINEL` should be **retained**: it still means "whoever the
CEO is now", and it is what keeps a persisted layout correct across a change
of default agent (`panelRegistry.ts:16`, `ChatPanel.tsx:20`).

### Note on `defaultId` vs CEO

They are deliberately decoupled (`useRosterEntries.ts:126-135`): the backend
picks the `role == "ceo"` agent as the container default but silently falls
back to index 0 when no agent carries that role (`config.py` `_build_agents_list`).
On such a company `defaultId` is an arbitrary agent and nobody gets the CEO
badge. A Friday-first UI makes that cohort more visible, not less.

Out of scope here — but the roster must not assume a CEO exists. If
`entries.find(isCeo)` is undefined, the collapsed state falls back to
`defaultId`, and if that is also absent, to the first ordered entry.

---

## §3 — Board default on Tasks

"Tasks" is the `issues` panel — `panelRegistry.ts:48` maps `issues: "Tasks"`;
`IssuesPanel.tsx:36-38` records that the route and the Paperclip model still
say "issues".

The board already exists and is live: `teams/inbox/KanbanBoard.tsx` (+
`KanbanColumn`, `KanbanCard`), ported from upstream Paperclip, real `@dnd-kit`
drag between columns, drag-end patching `status` only. Tested. `@dnd-kit` is
already a dependency (`package.json:21-23`). Its only consumer is
`TasksList.tsx:991`.

**D3.1 — Flip the default.** `TasksList.tsx:743`,
`useState<TasksViewMode>("list")` → `"board"`.

**D3.2 — Persist the choice**, same `inboxStorage` helper as §2. Not optional
padding: view mode is currently plain `useState` with no persistence, which is
invisible today only because the default is where most users stay. Flipping the
default without persisting means every list-preferring user re-picks on every
reload.

**Known cost, accepted:** Sort, Group-by and parent/child tree nesting are
hard-gated to list mode (`TasksList.tsx:907`), so defaulting to board hides
them on first load. The board's own header comment also defers density
controls, windowing and the sub-issue progress strip (`KanbanBoard.tsx:19-20`).

**Watch:** `KanbanBoard.tsx:84` silently drops any issue whose status is not
one of the seven `BOARD_STATUSES`. The frontend `IssueStatus` union is wider
(`teams/shared/types.ts:10-22` — also `pending`, `review`, `won_t_do`,
`duplicate`, `open`). On a list default that leak is cosmetic; on a board
default it means **tasks can be invisible by default**. Implementation must
either map the extra statuses onto columns or add an explicit overflow column.
This is the one real defect the flip exposes.

Mobile is unaffected: it has its own `TeamsMode` with board as a peer mode
defaulting to `overview` (`apps/mobile/src/app/(app)/teams/index.tsx:173,219`),
and its `BoardView` is a status-grouped `SectionList`, not a kanban.

---

## §4 — Landing copy

Two independent edits. Santi's screenshot was verified against `main` and
matches it exactly, including `first 200` (`launch_settings_repo.py:39`).

### §4a — Headline

`Type an idea.` / `Watch it become a company.` → **`If you have an idea,` /
`you deserve a team.`** (`HeroFunnel.tsx:92-95`)

Keep the hard `<br/>` after the comma. The two-line desktop fold is load-bearing:

- `Scene.tsx` derives `GLASS_FLOOR_Y` from where the headline's top edge
  projects, so the lamp "never hangs over the copy".
- `landing.css:1205-1211` derives the mobile `240px` top padding from a
  measured ~518px section height.

39 chars vs the current 40, same line count, so both stay valid with no
re-derivation. `landing.css:1223-1225` hides the `<br/>` under 720px; its
comment references an older headline ending in "you." and should be corrected
while we are in there.

Also update: `HeroFunnel.test.tsx:46` (exact `<h1>` assertion). No analytics
event carries the headline or CTA label as a property — `captureSignupCta`
sends `ctaId: "hero_funnel_submit"` (`HeroFunnel.tsx:73-74`) — so nothing is
orphaned.

### §4b — The pill

Current: `Launch competition · first ${comp_slots} get a month free → see the rules`
(`LaunchHeroPill.tsx:22`).

The critique, from a real user: the pill sells a discount above the fold before
the product is understood; the value offered is not the value the visitor came
for; "a month free" is generic.

**Honest constraint on any replacement:** the pot is
`$1,000 + $10 × entrants + per-seat for counted teams`
(`launch_phase.py:141-153`), and the winner is **judged**, not
highest-revenue — the launch design doc is explicit: *"Judges pick the winner.
Never promise 'highest revenue wins'."* So "We'll pay you $1,000" overclaims on
two axes. What is true: a pot of at least $1,000, growing, for the biggest
revenue lift over 30 days.

Three options to choose from at implementation time (owner's call, not the
implementer's):

- **A — lead with the pot:** `Launch competition · a $1,000+ pot for the biggest 30-day lift → the rules`
- **B — lead with the reader's business:** `Grow your business, win the pot · $1,000+ on the line → the rules`
- **C — lead with the stake:** `$1,000+ pot · biggest 30-day revenue lift wins → see the rules`

All three keep the pot honest, drop "a month free" from the fold, and keep the
comped month as a detail on `/launch` where the rules already live
(`LaunchPage.tsx:13-24`).

Note the pill only renders when the launch phase is `open` or `competition`,
and degrades to nothing on any non-200 (`LaunchHeroPill.tsx:18,27`;
`useLaunchInfo.ts:20-27`). The CTA label `Join the launch` is likewise
phase-driven (`HeroFunnel.tsx:116`) and is out of scope.

---

## Sequencing

| PR | Scope | Depends on |
|---|---|---|
| 1 | §4a + §4b | nothing |
| 2 | §3 | nothing |
| 3 | §1 + §2 | — (must be one PR: §2's singleton chat panel only makes sense once §1 removes per-agent tabs) |

§4 can ship immediately. Note prod frontend does **not** auto-deploy on merge
(`prod-DeployVercelProd` is `workflow_dispatch`-gated), so landing copy is not
live until someone promotes.

## Testing

- §1: `buildDefaultLayout.test.ts` (asserts the 3-panel layout — must change),
  `layoutStorage.test.ts` (version migration), `workspaceController.test.ts`,
  `workspaceShellUrl.test.ts`, `panelRegistry.test.ts`. No e2e touches the dock.
- §2: new coverage for `orderRoster(entries, pinnedIds)` — pure function, cheap
  to test exhaustively — plus collapsed/expanded rendering and the
  no-CEO fallback.
- §3: `TasksList.test.tsx:99,264` already cover the toggle; add a default-view
  assertion and a status-not-in-BOARD_STATUSES visibility test.
- §4: `HeroFunnel.test.tsx`, `LaunchHeroPill.test.tsx`.

## Out of scope

- Replacing dockview.
- Server-side sync of pins or view mode.
- The `defaultId`-without-CEO cohort (noted, not fixed).
- Mobile Teams modes.
- The `Join the launch` CTA label and the launch phase machinery.
- `browserNav.ts` (`normalizeBrowserUrlDraft`) — has no importer anywhere in
  the repo and is dead, but deleting it is unrelated cleanup.
