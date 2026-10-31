# Sessions sidebar: what exists, and prior art for lists of many live agents

Date: 2026-10-30. Read-only research. Repo facts come from `origin/main` of
`/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync` (fetched today); every
repo claim cites file:line. Web facts cite a URL; anything not confirmed by an
official page is marked **unconfirmed**.

## 1. What exists today

### 1.1 The shell and the sidebar's sections

- The global rail has exactly five places: Chat, Activity, Work, Library, Canvas
  (`apps/frontend/src/components/workspace/WorkspaceHeader.tsx:19-26`; comment
  at :16-17 says "The five places, and nothing else (spec 3.1)").
- The Chat place's left column is `RoomDirectory` (`nav aria-label="Conversations"`,
  `apps/frontend/src/components/messaging/RoomDirectory.tsx:509`). Its heading
  carries a Sidebar options menu with Sort (Recent activity / A–Z / Unread first),
  "Jump to next unread" and "Manage sections" (:515-523), the header actions (New
  session) and "New conversation" (:526-527).
- Group order, top to bottom: Starred · the viewer's custom sections (drag to
  reorder, `SortableSection` :102-108, max 20 sections :84) · Team (DMs and group
  DMs, roster rows included) · Channels · **Sessions** · External connections ·
  Archived · Drafts · "Invite someone" row. The room groups come from
  `roomSections.ts:21-52` (`groupRooms`, order at :31-38); Sessions is spliced
  between the head groups and `TAIL_GROUPS = {external, archived}`
  (`RoomDirectory.tsx:85-86, 539-541`); Drafts :544-556; Invite :558.
- Every group shares one header: chevron collapse button, `h3` label, optional
  badge (`groupHeader`, :327-340). Collapsed state and sort persist per
  `<userId>:<workspace>` in localStorage (`sidebarPrefs.ts:4-30`). A collapsed room
  group still shows the open room and an unread count badge (:445-451, :470).
- A room row (`RoomDirectoryRow.tsx:40-85`) shows icon/avatar, name, "Shared" /
  "Muted" / "Agent" tags, star, unread count, draft pencil, and a `…` menu
  (star, mute, mark unread, copy link, add people, move to section, leave).

### 1.2 The Sessions group

Rendered at `RoomDirectory.tsx:392-442`, present whenever a session handler or a
session exists (:392).

- **Header badge**: when collapsed, the live count; always the session-search
  button (:420). `SessionSearch` searches what was said inside shared sessions
  (dialog, 50 hits, `SessionSearch.tsx:18`) — it does not filter the list.
- **Which rows, in what order** (`packages/shared/src/messaging/localSessionLabels.ts`):
  - `workspaceSections` :155-165 orders the input: one bucket per person, "You"
    first then A–Z; inside a person live first, then newest activity, then id.
    Comment :152-154: "Not by status: ranking 'Working' first put a session the
    Mac wrongly thought busy above everything" (owner ruling 2026-10-13).
  - `sidebarSessionList` :205-216 then drops anything offline ≥ 3 days
    (`SESSION_ARCHIVE_AFTER_MS` :168, `sessionArchived` :171-172), moves
    viewer-archived rows out (:193-196: archived at a seq; new output brings it
    back), and ranks pinned (0) → live (1) → ended/offline (2, newest first).
    Pinned and live keep the incoming (per-person) order.
  - **Cap**: `SIDEBAR_SESSION_CAP = 5` (:186). Shown = first 5 + every pinned +
    the open one (`RoomDirectory.tsx:402`); a "+N more" / "Show fewer" toggle
    (:426-429) whose state is a plain `useState(false)` (:148), so it resets on
    reload. Below that: an "Archived N" fold (:430-435, closed by default), then
    "All sessions N" (:436-439) which opens `AllSessionsView` in the main pane.
  - Empty state: "No one is sharing a session." + Start session (:424).
- **The row** (`SessionListRow.tsx`), sidebar passes `oneLine` (:56):
  - Owner avatar with the tool logo in its corner (:79-82); title (bold when
    unread, shimmer while `busy`, :84); in one-line mode the signals
    ("2 agents running · PR #123 · checks passing", `sessionSignalsLine`
    `localSessionLabels.ts:106-107`) are appended inline (:84) and the second
    line (owner · why-ended / waiting-for / folder · signals) is dropped (:74).
  - Right edge: chat-unread count (:87); an orange dot while waiting (:88), a
    blue/"finished" dot for unread output (:89), else the time (:90, Happy's
    clock :23-34). Ended/offline rows are faded (:75). No status words (comment
    :44-50, ruling 2026-10-13).
  - `…` menu / right-click: Pin/Unpin, Mark unread/read, Archive/Unarchive
    (:92-106). Rename and End/Remove are only mounted when `!oneLine`, i.e. in
    All sessions, not the sidebar (:107-109). Everything is in the `aria-label` (:78).
- **State model** (`localSessionLabels.ts:43-51`): `here | background | waiting
  | done | offline | stopped`, with labels :75-79 ("Working"/"Idle",
  "Running in background", "Waiting on you", "Done", "Offline", "Stopped"), the
  primary action :82-87 (Open / Attach / Resume / none while offline),
  `sessionWaitingLine` :112-113 ("Waiting for input: permission prompt"),
  `endedWhy` :117-121 ("stopped while idle", "…'s login session restarted").
  A parked/superseded origin, a subagent and a spare never reach a client (:33-34).
- **Attention** is per viewer, per browser: last-seen seq per session
  (`sidebarPrefs.ts:32-52`), `sessionUnread` :249-253 ("finished" once idle/ended,
  else "unread"), `markSessionUnread/Read` :270-275. Pins and archives likewise
  (`sidebarPrefs.ts:54-77`; comment: "Nothing server-side"). The Dock/tab count
  is your own sessions at a prompt (`sessionsNeedingYou` :64-72), with
  per-kind notification prefs :54-60.
- **Row data gaps against the ask**: `LocalSession` (`types.ts:412+`) has
  `deviceId` (:424) but no machine name, and `cwdName` is a basename only
  (:471); `repo`/`branch` exist only on `LocalSessionPickup` (:548-550), not the
  row. So "computer" and "repo/branch" cannot be shown yet.
- **All sessions** (`AllSessionsView.tsx`): search box (`sessionMatches` over
  title/folder/tool/owner, `localSessionLabels.ts:278-283`), live rows first
  with no headings, then one collapsed "Ended N" bucketed Today / Yesterday /
  This week / Earlier (`sessionHistory` :221-239), "Remove all ended" for the
  owner (:46); a search opens Ended automatically (:38-39). Two-line rows with
  rename and remove.

### 1.3 How the cap and grouping got here

`git log origin/main -- RoomDirectory.tsx` (newest first), with the shared file:

| PR | What changed for the list |
| - | - |
| #2648 | Sidebar sessions, glyph, in-room and focus views (first appearance) |
| #3222 | Always-on Sessions group, Recent, unread dot, filter, owner notifications |
| #3253 | Offline 3 days = archived (read-time rule, still :168) |
| #3266 | Sidebar lists only live; All sessions holds the rest — **`SIDEBAR_SESSION_CAP` born here** (`git log -S`) |
| #3913 / #3927 / #3953 | Owner can remove an ended session; ended-on-purpose vanished |
| #4079 | "reads like Happy's: no status words, live first, ended folded away" (ruling 2026-10-13) |
| #4134 | Team first, one-line session rows, Invite row |
| #4140 | Slack-measure type and spacing |
| #4420 / #4425 / #4427 | One state per session; notification prefs + Dock count; agents-running and PR on the row |
| #4426 | Ended stays until archived (ruling 2026-10-27, "as on the phone and in both vendor apps"); Pin, Mark unread, Archive; cap kept at 5 but pinned + open rows exempt |
| #4428 | Session search button in the header |

Design docs on main matching sidebar/session: `docs/superpowers/plans/2026-10-01-native-session-parity.md`,
`docs/superpowers/specs/2026-10-02-managed-session-sharing.md`,
`…/2026-10-04-session-sharing-mod.md`, `…/2026-10-05-public-session-links.md`,
`…/2026-10-07-agent-session-approval.md`. The code cites a "spec 2026-10-26
(e)/(f)" (`localSessionLabels.ts:32, 93, 97`) that is not in `docs/` on main.

## 2. Prior art

### 2.1 Claude Code agent view (`claude agents`) — official
https://code.claude.com/docs/en/agent-view

- **Grouping**: by state, fixed order `Pinned · Ready for review · Needs input ·
  Working · Completed`; `Ctrl+S` switches to group-by-directory and the choice
  persists. "Ready for review" is driven by the PR, not the process state.
  `Completed` "collects finished, failed, and stopped sessions together" and
  "fills the space left after the live groups"; on a short terminal the header
  compacts "so working and needs-input sessions stay visible".
- **Attention**: Needs input is yellow and sits above Working; the tab title
  reads `2 awaiting input · claude agents`; inside a normal session the footer
  shows `← 2 agents` (count waiting on you, refreshed ~10 s) or `← 2 done`. A
  session holding a cross-session message for approval shows `approve message
  from <sender>` on its Needs-input row. Peek panel (`Space`) opens "with the
  sentence the row truncates, such as the exact question" and a `waiting 3m` line.
- **Overflow**: "Sessions that don't fit fold into a `… N more` row"; filters in
  the dispatch input (`s:blocked`, `a:<agent>`, `n:<text>`, `o:<text>`, `#1234`,
  a URL) and while a filter is active "all groups stay expanded"; `Ctrl+F` find;
  `Alt+↑/↓` jump between group headers; `Alt+1..9` attach to the n-th session
  in the focused directory; `Shift+↑/↓` manual reorder; `Ctrl+T` pin (pinning
  also keeps the process alive); `Ctrl+R` rename; `Ctrl+X` twice deletes.
- **Rows**: icon state (animated / yellow / dimmed / green / red / grey), name,
  one-line summary that "fill[s] the row's remaining width", PR badge coloured
  by review/checks state (`#1234` yellow/green/purple/grey).
- **Others' sessions**: none — "Sessions are local"; "Subagents and teammates
  aren't listed as separate rows."
- **Finished**: Completed group, age "freezes at how long the run took"; idle
  process stopped after ~1 h but conversation resumable; `/resume` picker shows
  deleted ones too; after shutdown, >48 h old sessions show `stopped` with a
  reason, and `Enter` asks before resuming "the weeks-old conversation".
- No stated maximum session count; limits section only names rate limits.
- Good: attention-first ordering, reason on the row, filters that expand
  collapsed groups. Bad for us: single machine, single person.

### 2.2 Claude Code Projects — Overview pane — official
https://code.claude.com/docs/en/claude-projects

- Threads tab "groups threads by state": `Ready for review · Waiting on you ·
  Working · Landing · Idle · Resolved`. Waiting on you = "need your reply or
  approval, or that failed". Resolved = marked done by you, by Claude after the
  last step (e.g. PR merged), "or automatically after a week with no activity";
  reopenable. The Overview button "shows a dot when a thread is waiting on you".
- Good: failed is treated as waiting-on-you; a resolved/auto-resolve bucket
  replaces manual archiving.

### 2.3 Claude desktop app, Code tab — official where stated
https://code.claude.com/docs/en/desktop.md

- Default grouping is **by folder**: "look for the newest session in the group
  for the folder it runs in"; hover a heading for **Group and sort**; earlier
  layout had **Group by / Sort by** and a `Status → Archived` filter; current
  layout reaches archived via **View all → Archived**. Archive from the row's
  hover menu; optional "Auto-archive after PR merge or close" (not for cloud or
  still-running sessions); worktrees cleaned ~30 days after archive.
- Multiple sessions = "Sidebar tabs"; `Ctrl+Tab` / `Cmd+Shift+]` cycle; Option-
  click opens split view, Cmd-click a new window. OS notification when a session
  finishes and you aren't viewing it. Local, SSH and Cloud are environment
  choices; the docs don't say how the sidebar marks them.
- **Unconfirmed** (third-party): sidebar "shows which sessions are working or
  waiting" and groups active/recent by project, filterable by status/environment
  (https://pasqualepillitteri.it/en/news/866/claude-code-desktop-redesign-parallel-sessions).

### 2.4 claude.ai/code web sessions — official
https://code.claude.com/docs/en/claude-code-on-the-web

- "Sessions appear in the sidebar"; row `⋯` menu: Archive, Delete, Open in,
  Continue in project. Archived live on a separate **Archived** tab at
  claude.ai/code/sessions; "Unarchive" returns a row. Team visibility is a
  per-session toggle (Private / Team); recipients "see the latest state when they
  open the link, but their view doesn't update in real time". A Remote-Control
  local session appears in the same sidebar; "To hide a session that already
  appears on your other devices, archive it in Desktop."

### 2.5 OpenAI Codex app (ChatGPT desktop) and cloud — mostly unconfirmed
https://learn.chatgpt.com/docs/app (redirect from developers.openai.com/codex/app)
https://learn.chatgpt.com/docs/reference/troubleshooting

- Official: sidebar lists **Chats** per project; "The sidebar lets you filter
  chats based on the state of a project"; missing chats → "select the filter icon
  next to Chats, then select Chronological"; archived chats live in Settings and
  unarchive "reappears in its original sidebar location"; projects can be
  removed from and re-added to the sidebar. Cloud docs only say the CLI can
  "list recent cloud chats and check their status".
- **Unconfirmed**: app 26.917 (2026-09-22) "improved status indicators for
  running threads, queued prompts, side chats, and subagents"; a "Remove from
  section" fix implies pinned sections
  (https://releasebot.io/updates/openai/codex, https://learn.chatgpt.com/docs/changelog).
  Past-thread search by content and branch name (https://www.gradually.ai/changelogs/codex-app/).
- Bad: repeated reports of the sidebar hiding threads that are on disk
  (https://github.com/openai/codex/issues/25084, https://github.com/openai/codex/issues/23900)
  — a list that silently caps is read as data loss.

### 2.6 Cursor Agents Window / cloud agents — thin official, forum for detail
https://cursor.com/changelog/3-0 · https://cursor.com/docs/agent/agents-window · https://cursor.com/docs/cloud-agent

- Official: the Agents Window runs "many agents in parallel across repos and
  environments" (local, worktree, cloud, remote SSH); cloud agents "are visible
  to members of the Cursor team they were started under", viewing is read-only
  and needs repo access; admins can enable team follow-ups. No official page
  describes the list's grouping or statuses.
- Forum (staff reply, **secondhand**): sidebar rows reflect "live state of agents
  blocked on approvals or pending plans, regardless of focus", while the Dock
  badge counts "agents with activity you haven't seen yet" — two different
  counts, acknowledged as "not ideal"
  (https://forum.cursor.com/t/dock-menu-bar-badge-count-does-not-match-agents-sidebar-needs-attention-glass/168982).
  Web sidebar filters are single-select: "My agents" / "Team agents" /
  "Collaborating on", and status "Merged" / "Closed"; users want multi-select
  (https://forum.cursor.com/t/show-both-my-agents-and-team-agents-on-web-sidebar/154417).
- Lesson: keep "needs you now" (live state) and "new since you looked" (unread)
  as two signals with one consistent definition each.

### 2.7 GitHub Copilot coding agent — official
https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/track-copilot-sessions
https://docs.github.com/en/copilot/how-tos/use-copilot-agents/manage-agents

- An agents panel "available from any page on GitHub"; home shows recent
  sessions under "Pick up where you left off" with **View all**. Your list holds
  sessions you started "and sessions where another user prompted Copilot".
  A repository's Agents tab has an **All sessions** view "visible to anyone with
  access to that repository". Cloud sessions are shared by default; local
  sessions unshared until shared view-only. Only stopped sessions can be
  archived; cloud sessions "can be archived but not deleted". Statuses, filters
  and limits are not documented.

### 2.8 Devin — API only; UI unconfirmed
https://docs.devin.ai/api-reference/v1/sessions/list-sessions.md · https://docs.devin.ai/openapi.yaml

- v1 statuses `working | blocked | expired | finished | suspend_*`, with
  `blocked` = waiting for user input; v3 adds `status_detail`
  `waiting_for_user | waiting_for_approval`; list filters by tags and creator
  email. No official page describes the web sessions list; treat any UI claim
  as unconfirmed.

### 2.9 Linear issue lists — official
https://linear.app/docs/display-options · https://linear.app/docs/filters

- Display options (`Shift+V`): group by status/assignee/project/…; sub-grouping;
  sticky group headers; each group header shows a count; "Show empty groups"
  toggle; ordering by status puts "active issues closest to done first, then
  completed and canceled"; display properties choose which fields a row shows;
  per-user, with "Set as default" for the workspace. Filters (`F`) are typed
  (`Assignee is …`), combinable with AND/OR, and the URL carries them.
- Lesson: separate *what is shown* (filters) from *how it is shown* (group,
  order, row fields), and let the viewer persist both.

### 2.10 Slack sidebar — official + Slack blog
https://slack.com/help/articles/360043207674-Organize-your-sidebar-with-custom-sections
https://slack.com/help/articles/212596808-Customize-your-sidebar
https://slack.com/blog/productivity/neatly-organize-your-work-life-with-custom-sidebar-sections-in-slack
https://slack.com/help/articles/226410907-View-all-your-unread-messages

- Sections are personal ("only visible to you"); each can override the global
  filter/sort: "Active only" hides "conversations with no new activity in the
  past 30 days", sort A–Z / Recency / Priority, and "within each sidebar section,
  you can choose to exclusively see items with unread messages". Bulk "Move
  to…"; "Clean up your channel list" by last-viewed age. Unreads view
  (`⌘⇧A`) groups by conversation with collapsible headers, `Esc` marks a group
  read, sortable "like your sidebar" or by newest/oldest activity.
- Collapsed-section unread badge is **unconfirmed** by an official page
  (third-party guides say a collapsed section shows an unread count).

### 2.11 VS Code Agent Sessions — official
https://code.visualstudio.com/updates/v1_106 · https://code.visualstudio.com/docs/copilot/chat/chat-sessions
https://code.visualstudio.com/learn/foundations/agent-sessions-and-where-agents-run

- v1.106: one view "divided into sections for local chat sessions … and for
  background agent sessions" (Copilot coding agent, Copilot CLI, Codex), search
  `⌥⌘F`, optional `single-view` "consolidated view … across all providers".
- Current docs: Chat-view list "grouped by time" for the workspace; Agents
  window lists across workspaces, grouped "by workspace (the default) or by
  timeframe", collapsible headers, pin to top on hover, custom groups and
  subgroups; filter toggles active vs **Archived** / **Done**; an **External**
  filter (None / Recent / 24h / 7d default / 30d) with "Recent" showing "up to
  two most recently updated external sessions"; rows carry status, harness
  and "a file change count if the agent made changes you have not reviewed
  yet" that "clears once you accept or undo them". Settings auto-mark merged
  sessions done after N days and auto-delete after a grace period.

### 2.12 Zed agent panel — official
https://zed.dev/docs/ai/agent-panel.md

- Threads sidebar (`cmd-alt-j`) shows "all your threads grouped by project";
  Thread History is "all your threads across all projects, sorted
  chronologically"; `ctrl-tab` cycles recent threads; hover-archive or
  `shift-backspace`; notify-when-waiting and sound-when-done settings. No
  status or unread indicators in the list are documented.

### 2.13 Warp Agent Management Panel — official
https://docs.warp.dev/platform/managing-cloud-agents.md · https://docs.warp.dev/agents/capabilities/agent-notifications.md

- Lists interactive agents and cloud runs; statuses `Working · Blocked · Canceled
  · Failed/Errored · Success`, Blocked = "waiting on user input or a required
  step"; **Personal** vs **All** tabs (All adds teammates' shared cloud runs);
  filters by source, day, creator, status; local child agents "don't appear as
  separate rows", cloud children do. Per-tab status icon plus an attention
  badge; a bell "notification mailbox" with All / Unread / Errors filters and
  "Mark all as read"; notifications mark read when you open the tab.

## 3. Patterns worth taking and why

1. **Group by state with "waiting on you" above "working", and put failed/stopped-
   with-reason in the waiting bucket** — Claude Code agent view (Needs input above
   Working) and Projects ("Waiting on you … or that failed"). Maps to our
   `waiting` state and `endedWhy`; replaces today's person-first order
   (`workspaceSections`), which buries a teammate's prompt under your idle rows.
   Keep the 2026-10-13 worry (a wrongly-busy session jumping the queue) by
   ranking only `waiting`, not `busy`.
2. **Second axis the viewer can switch and that persists** — agent view `Ctrl+S`
   state ↔ directory; Linear group-by; VS Code workspace ↔ timeframe. For us:
   state ↔ person ↔ repo/folder, stored in `sidebarPrefs` beside `collapsed`.
3. **Fold per group, not one global cap** — agent view's "… N more" per group,
   Completed "fills the space left", Linear's per-group count in the header.
   Replace `SIDEBAR_SESSION_CAP = 5` with a per-group fold that always keeps
   waiting rows and pinned rows visible; a collapsed group shows its count
   (we already do that for rooms, `RoomDirectory.tsx:470`).
4. **Typed filters that expand collapsed groups** — agent view `s:blocked`,
   `a:<agent>`, `n:<text>`, `#1234`; Linear `F`. We have `sessionMatches`
   (title/folder/tool/owner) only in All sessions; move it into the sidebar and
   add person, state, harness and PR number as tokens.
5. **Two attention signals, each with one definition** — Cursor's sidebar
   (live blocked state) vs Dock badge (unseen activity) mismatch is the warning.
   Our `waiting` (orange, `sessionsNeedingYou`) and `unread`/`finished` (blue)
   already differ; make the group header count and the Dock count use the same
   rule and say which ("2 waiting on you").
6. **The reason on the row and the full question one key away** — agent view's
   `approve message from <sender>` and the peek panel's "exact question" with
   `waiting 3m`. Our `waitingFor` exists but the one-line sidebar row drops it
   (`SessionListRow.tsx:74`); show it as the row's second line only for waiting
   rows, or as a hover/peek.
7. **PR badge coloured by review state, as a filter key** — agent view (`#1234`
   yellow/green/purple/grey, "Ready for review" group driven by the PR). We have
   `sessionPrLabel` as text; colour it and let a "Ready for review" bucket exist
   when the PR is open and checks pass.
8. **Mine / Team / All as a one-click scope, multi-select allowed** — Warp
   Personal vs All, Cursor My/Team/Collaborating (single-select, users complain),
   GitHub "sessions you started … and where another user prompted". Maps to
   `own`/`ownerName` and to network-only sessions; default to Mine + waiting
   rows from anyone.
9. **Finished rows auto-resolve instead of waiting for manual archive** —
   Projects "Resolved … automatically after a week with no activity"; Claude
   desktop "Auto-archive after PR merge or close"; VS Code auto-mark-done after N
   days. Our ended rows stay until archived (ruling 2026-10-27) and offline rows
   vanish at 3 days; add a merged-PR or N-days rule, and keep "Resume" for the
   owner from the resolved bucket.
10. **Unreviewed-changes count that clears when reviewed** — VS Code's file-change
    count. We already carry `changes` (`types.ts:454`) and `changesLabel`; it is a
    better "new output" signal for a teammate's session than a blue dot.
11. **Never silently hide rows that exist** — Codex's hidden-threads bug reports
    and agent view's `/resume` picker that "includes deleted ones". Any fold must
    say "+N more" and the All sessions count must equal what the server has.
12. **Per-section "Active only" window and "unread only" toggle** — Slack's
    30-day activity filter and per-section unread-only view. Maps to idle
    teammate sessions and workspace agents: an "Idle" fold that hides rows
    quiet for > X hours unless pinned.

## 4. Open design questions for the founder

1. Primary grouping: by state (waiting · working · ready for review · idle ·
   ended) with person as a secondary toggle, or keep person-first and add a
   "Waiting on you" strip above it?
2. Whose waiting rows count toward your Dock badge and the header count: only
   sessions you own (today), or also a teammate's session that asks for *you*
   (`waitingFor` naming you) and workspace agents (Friday, Forge)?
3. Do workspace agents and network-only (unopenable) sessions live in the same
   list with a disabled Open, or in their own group below the openable ones?
4. Retention: auto-resolve ended rows after merge or N days (Projects / VS Code
   style), or keep "until the owner archives" (2026-10-27 ruling)? And should
   pins, archives and seen-marks move server-side so the phone and web agree?
5. Which row fields are mandatory on one line at sidebar width — person, harness
   logo, title, folder, state, PR, agents, unread — and do we add machine name
   and repo/branch to `LocalSession` (missing today) to show them at all?
6. Is the sidebar the only list, or does a wide "All sessions" pane become the
   Linear-style table (filters, group-by, display properties) while the sidebar
   stays a short attention-first digest?
