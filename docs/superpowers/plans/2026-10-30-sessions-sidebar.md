# Sessions sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Replace the five-row session cap with folding groups, Needs you, a persisted view switch and a filter, built on `SessionRow`.

**Architecture:** One pure module (`sessionGroups.ts`) decides groups and folds. `SessionListRow` renders one `SidebarRow`. `RoomDirectory` and `AllSessionsView` compose them. `MessagingWorkspaceContext` builds `SidebarRow[]` from `useSessionRows`.

**Tech Stack:** React, TypeScript, vitest + testing-library, plain CSS in `messaging.css`.

**Spec:** `docs/superpowers/specs/2026-10-30-sessions-sidebar-design.md` (rules and states); `docs/superpowers/specs/2026-10-30-one-session-list-design.md` §10.

## Global Constraints

- Never change `useSessionRows`'s body or `SessionRow`'s shape (`packages/shared/src/messaging/sessionRow.ts`).
- Actions read only `row.backend`. A row without one has no Open, Join, Resume or End.
- Only `waiting` (and own failed ended rows) move. Never rank by `busy`.
- Focused tests only: `cd apps/frontend && npx vitest run <files>`. Never a full suite.
- Never `git stash`. `git add` named paths only; `apps/frontend/.step4-harness` is someone else's scratch.
- Colours only from existing CSS tokens. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- A `localStorage` value of the wrong type for a new pref: falls back to defaults (Task 1 test).
- A query with regex characters or `#4527`: literal match (Task 1 test).
- Signed-out viewer (`isViewer` false everywhere): no Needs you, no crash (Task 1 test).
- A pinned row beyond the fold, and the open session in a collapsed group: stay visible (Task 1 test, Task 3 test).
- A network row: no element with role button named `Open …` (Task 2 test).

---

### Task 1: The grouping module and prefs

**Files:**
- Create: `apps/frontend/src/components/messaging/sessionGroups.ts`, `__tests__/sessionGroups.test.ts`
- Modify: `apps/frontend/src/components/messaging/roomSections.ts` (add `SidebarRow`), `sidebarPrefs.ts`, `__tests__/sidebarPrefs.test.ts`; `packages/shared/src/messaging/localSessionLabels.ts` + `packages/shared/src/index.ts` (extract `groupByDay`)

**Interfaces — Produces:**
```ts
// roomSections.ts
export interface SidebarRow { row: SessionRow; owner: RoomParticipant; unread?: "unread" | "finished" | null }
// sidebarPrefs.ts: SidebarPrefs gains
sessionGroupBy: SessionGroupBy; sessionShow: SessionShow; sessionsExpanded: string[];  // defaults "person", "all", []
// sessionGroups.ts
export type SessionGroupBy = "person" | "state" | "repo";
export type SessionShow = "all" | "mine" | "live";
export const SESSION_GROUP_FOLD = 5;
export const NEEDS_YOU_GROUP = "needs-you"; export const NETWORK_GROUP = "network";
export interface SessionGroup { id: string; label: string; entries: SidebarRow[] }
export const sessionRowKey: (row: SessionRow) => string;       // backend?.sessionId ?? id; the key for marks and current
export function sessionRowFailed(row: SessionRow): boolean;    // ended && (engine?.job?.state === "failed" || endedReason === "login-session-ended")
export function sessionRowNeedsYou(row: SessionRow): boolean;  // person.isViewer && (waiting || failed)
export function sessionRowGone(row: SessionRow): boolean;      // offline or ended
export function sessionRowAt(row: SessionRow): number;         // backend.lastEventAt ?? backend.endedAt, else endedAt ?? lastSeenAt ?? 0
export function sessionRowMatches(row: SessionRow, query: string): boolean;
export function ownWaitingCount(rows: readonly SessionRow[]): number;  // isViewer && state === "waiting"
export function sessionRowGroups(entries: readonly SidebarRow[], options: { groupBy: SessionGroupBy; show: SessionShow; query: string; marks: SessionMarks; now?: number }):
  { groups: SessionGroup[]; archived: SidebarRow[]; hidden: number; listed: number };
export function foldedEntries(entries: readonly SidebarRow[], options: { limit: number; keep: (entry: SidebarRow) => boolean }): SidebarRow[];
// shared: groupByDay<T>(items, at: (item: T) => number, now?) -> { key; title; data: T[] }[], with sessionHistory delegating to it. Exported from index.ts.
```

Rules for `sessionRowGroups`, in order:
1. Drop (not listed, not archived, not counted): `!row.engine && row.backend && sessionArchived(row.backend, now)`. `listed` is what remains.
2. Archived: `sessionArchivedByViewer({ sessionId: key, latestEventSeq: backend?.latestEventSeq }, marks.archived)`, except an own waiting row, which is never archived. Return them in `archived`, unfiltered.
3. Query: `sessionRowMatches` filters everything else, including Needs you.
4. Needs you first (exempt from Show). `hidden` counts rows Show removed: `mine` removes non-viewer rows; `live` removes `sessionRowGone` rows.
5. `source === "network" && !isViewer` go to `NETWORK_GROUP`, label `On the network`, last, in every view.
6. Rest by axis. Person: id `person:<userId ?? name>`, label `You` for the viewer else name, viewer first then A–Z. State: id `state:<state>`, labels and order Waiting, Working, Idle, Done, Parked, Offline, Ended. Repo: id `repo:<repo ?? folder>`, A–Z, then id `repo:` label `No repo`.
7. Inside every group: pinned (by `sessionRowKey`), then not-gone, then gone; within each rank `sessionRowAt` descending, then key. Empty groups dropped.

`sessionRowMatches`: lower-cased `includes` over title, folder, repo, branch, `sessionToolName(agent)`, agent, person name, computer, the state word (`waiting working idle done parked offline ended`; `failed` when failed), waitingFor, and `#<pr.number>` from backend. Empty query matches.

`foldedEntries`: the first `limit` entries plus every later one where `keep`, order preserved. `limit: 0` is the collapsed case.

- [ ] Write `sessionGroups.test.ts` with a `row()` builder covering: Needs you holds own waiting and failed only, teammate's waiting stays under the teammate; order is not by busy (an older busy row sits below a newer idle one, both live); viewer first then A–Z; network rows in their group in all three axes; own network row under You; state and repo axes incl. `No repo` last; pinned first; Show mine and live with `hidden` count and Needs you exempt; query `#4527`, `(` and `waiting`; own waiting not archivable; archived row returns; offline-3-days dropped only without engine; signed-out viewer yields no Needs you; `foldedEntries` keeps pinned past the limit and handles limit 0; `ownWaitingCount`.
- [ ] Add a `sidebarPrefs.test.ts` case: wrong-typed `sessionGroupBy`, `sessionShow`, `sessionsExpanded` read as defaults; good values round-trip; old stored prefs without the fields read as defaults.
- [ ] Run, see them fail. Implement. Keep `sessionHistory`'s behaviour; its existing test must pass.
- [ ] Run `cd apps/frontend && npx vitest run src/components/messaging/__tests__/sessionGroups.test.ts src/components/messaging/__tests__/sidebarPrefs.test.ts` and `cd packages/shared && npx vitest run src/messaging/sidebarSessionList.test.ts` plus any test of `sessionHistory` found with grep. Pass.
- [ ] Commit.

### Task 2: The row

**Files:** Modify `SessionListRow.tsx`, `messaging.css`, `__tests__/SessionListRow.test.tsx`.

**Interfaces — Consumes:** Task 1. **Produces:**
```ts
SessionListRow({ entry, current, onSelect, onRename, onRemove, onShare, oneLine, marks }: {
  entry: SidebarRow; current: boolean; onSelect?: (sessionId: string) => void; oneLine?: boolean;
  onRename?; onRemove?;   // as today
  onShare?: () => void;   // shown only for !row.backend && person.isViewer
  marks?: SessionRowMarks; })
export function sessionAge(at: number | undefined, now = Date.now()): string  // "now", "5m", "2h", "3d"; "" when undefined
```
Behaviour (states table in the spec):
- Own `sessionRowState` word at the right edge: done `Done`, offline `Offline <age>`, parked `Parked`, ended `Ended <age>`, failed `Failed <age>` (class `session-row-state--failed`, colour `--err`); otherwise time as today. Waiting dot, then unread dots, win over the word and time; a row with `oneLine` false also keeps the word next to a dot.
- Waiting dot colour moves to `--brass` (`--warn` is gray in the chat theme, so waiting was unreadable next to unread) and 8 px.
- `oneLine` second line, only when news: waiting → `waitingFor ?? "Waiting for input"`; failed → `endedWhy(...) ?? "Job failed"`; live with signals → `[folder, sessionSignalsLine(backend)]` joined ` · `; else none. Remove the inline signals from the title. Non-`oneLine` second line (All sessions): `[not viewer ? person : null, computer, repo with branch, folder, reason or waiting line, signals, canResume && !backend ? "resume with lightbulb who" : null]`.
- `title` on the entry: multi-line hover with person, harness, computer, repo and branch, folder, state with last seen or reason, `Resume with lightbulb who` when `canResume` and no backend.
- `aria-label` keeps today's shape `Open <tool · title>, <who>, <where>, <state>…`. State text: `sessionStateLabel(backend)` when no engine joined (web wording unchanged), else from `row.state`: Working, Waiting on you, Idle, Done, `Offline, last seen <age> ago`, `Parked, wakes when messaged`, Ended; plus the reason.
- No backend: the entry is a `div.room-directory-entry.session-row-info`, not a button, with the same contents and an `sr-only` span carrying the aria-label text minus "Open". Classes: `session-row--gone` for offline and ended; `session-row--network` (entry opacity .75, no hover fill) when no backend.
- Share: a small ghost button "Share" with aria-label `Share <title>`, right edge, revealed on row hover and focus-within, taking the state's place; `@media (hover: none)` always shown.
- Menu: Pin and Archive for all rows with `marks`; Mark unread only with a backend. `sessionTitle` reads from `row.title`.
- Reclaim width: with a menu, `.session-row` is `position: relative`; `.room-directory-actions` absolutely positioned right, and `.session-row-end` (wrapper of dot, time, word, chat count) gets `visibility: hidden` on hover, focus-within and when menu is open. `@media (hover: none)`: back in flow, reserved. Keep touch floor rule at `messaging.css` ~427 working.
- Busy shimmer is `row.state === "busy"`. Bold when `unread`.

- [ ] Rewrite the test file's builder to `SidebarRow` via `mergeSessionRows` for backend rows and a literal for network rows. Keep every existing behaviour case. Add one per state: waiting reason on second line and dot; idle time; done; offline `Offline 2h` and aria `last seen`; parked; ended with reason in `title`; failed; network row has no `Open` button, no Mark unread, has Pin; own network row with `onShare` shows Share and calls it, without `onShare` none; teammate's network row never Share; canResume network row hover mentions `lightbulb who`; second line absent on a plain idle row; folder and PR on second line.
- [ ] Fail, implement, pass: `cd apps/frontend && npx vitest run src/components/messaging/__tests__/SessionListRow.test.tsx`. `AllSessionsView` and `RoomDirectory` may not typecheck until Tasks 3–4; that is expected.
- [ ] Commit.

### Task 3: The Sessions section and wiring

**Files:** Modify `RoomDirectory.tsx`, `messaging.css`, `MessagingWorkspaceContext.tsx`, `__tests__/RoomDirectory.test.tsx`.

**Interfaces — Consumes:** Tasks 1–2. **Produces:** `RoomDirectoryProps.sessionRows?: readonly SidebarRow[]` replacing `sessions`; `onShareSession?: (row: SessionRow) => void`. `directoryProps.sessions` from the context stays (SessionFocus, All sessions until Task 4 read it) and is no longer read by `RoomDirectory`; keep it declared on the type if the context's object is typed by it. Context adds `sessionRows` to `directoryProps`.

Behaviour (layouts in the spec):
- Context: `SidebarRow[]` memo from `useSessionRows`' result: `owner: ownerFace(person.userId ?? row.id, viewer not on roster ? myName : person.name)` (same name logic as `sidebarSessions`; a viewer row's `person.name` may be "Workspace member"), `unread: backend ? sessionUnread(backend, seen) : null`. Do not touch the existing `sidebarSessions` memo or the hook. Dock: `needingYou` becomes `notifyPrefs.waiting === "off" ? 0 : ownWaitingCount(sessionRows)`.
- Remove `SIDEBAR_SESSION_CAP`, `sidebarSessionList`, `sessionsExpanded` state from `RoomDirectory`; the shared exports stay.
- Header badge: collapsed `room-count session-count--waiting` (brass fill, foreground readable in both themes) with `ownWaitingCount` and aria `N waiting on you`, then today's live count; always `SessionSearch`, and when rows exist a `SlidersHorizontal` icon-sm menu `aria-label="Session view"` with `DropdownMenuLabel` Group by (Person, State, Repo or folder) and Show (All, Mine, Live) radio groups, writing through `updatePrefs`.
- Filter field: `input[type=search].session-filter`, `aria-label="Filter sessions"`, placeholder `Filter sessions`; shown when `listed > SESSION_GROUP_FOLD` or a query exists; Escape clears. Local state.
- `hidden > 0`: `room-directory-subgroup` button `<hidden> others hidden · Show all` (Live: `<hidden> ended or offline hidden · Show all`), sets Show to all.
- Groups: `<div class="session-group"><button class="session-group-header" aria-expanded aria-controls>chevron, label, count</button><ul aria-label=label>…</ul>`. Needs you: static header with no chevron, class `session-group-header--needs`, no collapse and no fold. If `groups.length === 1` no header at all and no collapse for it, but it still folds. Collapse state in `prefs.collapsed` under `sessions:<id>`.
- Fold: `foldedEntries(entries, { limit: query ? Infinity : collapsed ? 0 : expanded ? Infinity : SESSION_GROUP_FOLD, keep })`, keep = own or teammate `waiting`, pinned, or open session. Below it, a button with class `session-group-more` aligned to the title column: `… N more`; when expanded and over the fold, `Show fewer`. Expanded ids in `prefs.sessionsExpanded`. No more button when collapsed.
- Query with no results: `No sessions match "<query>".` role status and a Clear button.
- Empty (`listed === 0`): today's empty state. Archived fold and All sessions (`listed`) stay below, unchanged except archived rows render via the new row.
- Marks stay per browser, keyed `sessionRowKey`, today's logic. `onShare={onShareSession && (() => onShareSession(row))}`.
- CSS: `.session-group-header`: flex, min-height 22px, `--text-caption`, weight 500, muted-foreground, `padding: 0 8px 0 4px`, radius 6, hover fill as `.room-directory-entry`; count `margin-left:auto`, tabular. `--needs` foreground weight 600. `.session-group + .session-group { margin-top: 6px }`. `.session-group-more` padding-left 36px, caption, muted; hover foreground. `.session-filter` 28px high, like `.all-sessions-search`, margin `2px 8px 6px` and `width: calc(100% - 16px)`. Focus-visible uses `--ring`.

- [ ] Update `RoomDirectory.test.tsx`: a local `toRows(entries, viewerId?)` helper builds `sessionRows` from its `SidebarSession` fixtures with `mergeSessionRows`. Replace the cap test and keep the intent of the others. Add: 7 rows of one person fold to 5 with `… 2 more`, open and pinned past it stay; expanding survives remount; group by state persists after remount; collapsed person group keeps count and its waiting row; Needs you on top with own waiting, teammate's waiting under teammate; On the network below people with `Parked` row; Mine shows `N others hidden` and Show all restores; query opens collapsed groups and Escape clears; no match message; one person gets no sub-header; collapsed Sessions shows waiting pill; 80 rows render in one sitting with all groups present.
- [ ] Fail, implement, pass: `cd apps/frontend && npx vitest run src/components/messaging/__tests__/RoomDirectory.test.tsx src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx`.
- [ ] Commit.

### Task 4: All sessions

**Files:** Modify `AllSessionsView.tsx`, `MessagingWorkspace.tsx`, `__tests__/AllSessionsView.test.tsx`.

**Interfaces — Consumes:** Tasks 1–3, `groupByDay`. **Produces:** `AllSessionsView` prop `entries: readonly SidebarRow[]`, read from `directoryProps.sessionRows`.

Behaviour: unchanged layout; live first, then Ended by day. Live: not `sessionRowGone` (offline rows go under Ended as today, heading stays). Search with `sessionRowMatches` over rows not dropped. Filter out the rows `sessionRowGroups` would drop (use a shared exported predicate `sessionRowDropped(row, now)` from Task 1's module; add it). "Remove all ended" counts `person.isViewer && backend?.status === "ended"`. Rows use `SessionListRow` two-line with Rename and Remove when backend, per Task 2; `onOpenSession` gets backend session id. Network rows appear, inert. A viewer's archived rows stay out as today only for `sessionArchived`, not for marks.

- [ ] Update fixtures through `mergeSessionRows`; keep existing cases. Add: a network row lists with computer and repo and has no Open button or Remove; `#4527` query finds a PR row; search opens Ended.
- [ ] Fail, implement, pass: `cd apps/frontend && npx vitest run src/components/messaging/__tests__/AllSessionsView.test.tsx`.
- [ ] Typecheck the frontend once for changed files: `cd apps/frontend && npx tsc --noEmit -p . 2>&1 | grep -E "components/messaging/(SessionListRow|RoomDirectory|AllSessionsView|sessionGroups|MessagingWorkspace|sidebarPrefs|roomSections)" | head -30`. No errors in those files. Fix any test-file type errors there too.
- [ ] Commit.
