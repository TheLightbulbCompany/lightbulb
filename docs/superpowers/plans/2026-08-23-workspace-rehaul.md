# Workspace rehaul — plan and remaining work

One document. Supersedes the two earlier drafts on the `feat/workspace-*`
branches, which framed reskins as new builds and over-counted the work.

**Design:** `~/Desktop/isol8-chat-previews/` — 13 screens. `index.html` is the
hub, `MAP.html` is the panel-by-panel inventory, `STATUS.html` is the live
progress view. Iteration 1 is frozen at `~/Desktop/isol8-chat-previews-v1/`.

---

## The correction that shapes this plan

Nearly every screen in the design already has a **working panel behind it**.
`IssuesPanel`, `GoalsPanel`, `ProjectsListPanel`, `RoutinesPanel`,
`OrgChartPanel`, `UsagePanel`, `ActivityPanel`, `AgentInspectorPanel`, the file
viewer and the browser panel all function today. They are styled the old way and
scattered across separate destinations, but the data paths, handlers and
mutations exist.

So this is **ten reskins and three new things**, not twenty features. A reskin
here means: same read, same handlers, same mutations — new surface, new
grouping, new motion. If a PR in section A finds itself adding an endpoint, the
scope is wrong and it should stop.

Three earlier scope calls were wrong and are corrected below, because each one
would have caused duplicated work:

1. **The `browser` PanelKind already exists.** `BrowserDockPanel` renders a
   noVNC live view, singleton-keyed to the container's one Xvfb display, and
   `useBrowserToolAutoOpen` surfaces it when an agent picks up the browser tool.
2. **Activity already has a real feed** off the BFF's `/teams/activity`. The gap
   was display enrichment, now fixed in #1994.
3. **`AgentInspectorPanel` already has five tabs**, Channels among them.

---

## Open pull requests

`#1979 → #1980 → #1981` are stacked and must merge in that order. `#1994` is
independent and can merge any time.

| PR | Branch | What it does | State |
|---|---|---|---|
| [#1979](https://github.com/Isol8AI/isol8/pull/1979) | `feat/workspace-shell-rehaul` | Motion foundation, dock chrome, and a width floor for dock groups | CI green |
| [#1980](https://github.com/Isol8AI/isol8/pull/1980) | `feat/workspace-rail-restructure` | Session disclosure animation, Connectors promoted into the rail | Open |
| [#1981](https://github.com/Isol8AI/isol8/pull/1981) | `feat/workspace-completion-beats` | Completion beat on a resolved approval | Open |
| [#1994](https://github.com/Isol8AI/isol8/pull/1994) | `feat/activity-name-enrichment` | Agent names on the Activity page instead of raw ids | Open |

### Two real bugs fixed along the way

- **Dock groups had no width floor.** `groupConstraints` guarded height, but
  `layoutStorage` persists sash positions verbatim — so one drag past the edge
  left a panel too narrow to render and every reload restored it. (#1979)
- **Sessions could never have animated.** `AgentRoster` mounted the wrapper and
  its content in the same commit, so `grid-template-rows` had no start value to
  transition from. No CSS on the list could have fixed it. (#1980)

### Housekeeping for the stack

- #1981 carries the two superseded plan drafts. Delete them from that branch;
  this file replaces both.
- Merge #1979 first. GitHub retargets #1980 to `main` automatically, then #1981.

---

## A. Reskins — ten PRs, no backend

Same data, same handlers. Ordered by risk, lowest first.

| # | Work | What already exists | Risk |
|---|---|---|---|
| A1 | **Agent page**: restyle the five tabs, add a Files tab | `AgentInspectorPanel`, the EFS file viewer | Low |
| A2 | **Routines** → own rail destination, expandable steps | `RoutinesPanel`, `RoutineTriggerSummary` | Low |
| A3 | **Connectors** as one page | `integrations` + `llm` control panels | Low |
| A4 | **Marketplace** reskin | Marketplace drawer + storefront | Low |
| A5 | **Work**: three panels behind one scope switch | `IssuesPanel`, `GoalsPanel`, `ProjectsListPanel` | Med |
| A6 | **Work**: group by agent and project | `IssuesPanel` | Med |
| A7 | **Work**: create flow, status glyph plus inferred chips | Existing create paths | Med |
| A8 | **Dashboard**: scope switch over Company, Activity, Ledger | `OrgChartPanel`, `ActivityPanel`, `UsagePanel` | Med |
| A9 | **Company brain**: folder tree over the file surface | file viewer, `teams-artifacts` (read-only) | Med |
| A10 | **Rail**: one overflow per row, one state glyph | `AgentRoster` | **High** |

**A10 is the one to be careful with.** `ChatLayout.css` is dual-owned by the
desktop dock and the mobile shell, and 13 test files assert on `.agent-item`'s
structure. It gets its own PR with mobile explicitly regression-tested, and it
should go last.

**A9 is read-only** until the backend item below lands. Folders render; creating
one and uploading do not.

---

## B. Genuinely new — three things

| Item | Why it is new | Blocks |
|---|---|---|
| **Task due dates** | `Issue` has no `dueDate` field. New end to end: Paperclip schema → BFF projection → `Issue` type → Work surfaces → Calendar. | Half of Calendar |
| **Today digest** | "Needs you / working / finished" is a new read model, not a rearrangement of `teams-dashboard`. | The default landing surface |
| **Calendar** | No equivalent panel exists. | — |

### Calendar splits three ways

- **Ships today, no backend.** Routine schedules (`cronExpression`, `nextRunAt`)
  and run history (`RoutineRun.completedAt`, `failureReason`), plus agent runs
  (`HeartbeatRun`). This alone is a real calendar.
- **Derivable, with care.** Task completions. `Issue` has no `completedAt`, and
  `updatedAt` is a bad proxy — any edit bumps it, so a task edited last week
  lands on the wrong day. The **activity feed** is the honest source: it dates
  the status change itself. Depends on #1994.
- **Needs backend.** Due dates. See above.

### Company brain writes

Folder creation and upload endpoints. The EFS file viewer and `teams-artifacts`
cover the read path.

**Open the due-dates and Today-digest tickets now.** They have the longest lead
time and everything else can proceed without them.

---

## C. Unowned — ships broken if nobody takes it

Not features, which is exactly why they get skipped.

- **Empty states, every surface.** A workspace with no agents, no tasks, no files
  and no routines is the first thing a new customer sees, and none of that copy
  exists. This rehaul makes it *worse*: a dashboard built around "what needs you"
  is emptier on day one than a task list was. One pass, all surfaces, written
  together so they share a voice. Do it last, once every surface exists.
- **Provisioning.** Bind the stepper to real phases rather than a timer, and
  survive a step failing. Two states, not one: a first provision runs minutes and
  earns a narrative; waking a scaled-to-zero container runs seconds and must not
  pretend otherwise.
- **Mobile.** Every decision so far is desktop-only. `ChatLayout` is the mobile
  shell and shares CSS with the dock, which is why A10 is rated High.
- **Tauri shell.** Inherits `/chat` for free. Does *not* inherit window chrome,
  the tray, or `DesktopUpdater`'s footer placement once the rail moves. Verify
  after A10.

**Explicitly out of scope:** chat streaming polish. Likely the largest single
contributor to the app feeling slow, and independent of everything here.

---

## Settled decisions

| Question | Decision |
|---|---|
| `dreams`, `skill-proposals` | Stay in Settings. Out of scope. |
| `DesktopUpdater` | Already home in the sidebar footer. No action. |
| Wordmark | Keep Lora. Georgia stays the fallback. |
| Calendar's purpose | Organization: completions and due dates. Not external sync. |

---

## Suggested order

```
merge #1979 → #1980 → #1981, and #1994 whenever
open backend tickets: task due dates, Today digest
A1 A2 A3 A4        ← low risk, start in parallel
A5 → A6 → A7       ← Work, in order
A8  A9             ← Dashboard, Company brain (read-only)
Calendar tier 1    ← routines only, independent
A10 → mobile → Tauri
Calendar tiers 2+3, Today digest, brain writes  ← as backend lands
C: empty states, provisioning                   ← last
```

## Done means, per PR

`vitest run` green, `eslint` clean on touched files, `tsc --noEmit` adding no new
errors, and every control in `MAP.html`'s button inventory either wired or listed
in the PR body as deliberately deferred.
