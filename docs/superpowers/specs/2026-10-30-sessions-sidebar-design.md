# Sessions sidebar (design)

Step 4 of `2026-10-30-one-session-list-design.md`; decisions in its §10.5, research in `docs/research/2026-10-30-sessions-sidebar-design-research.md`. An extension of the existing sidebar, not a new look: same tokens, row measure (28 px, `--text-ui`), group header, avatar with harness mark, shimmer, faded-when-gone, and `…` menu. Built against `SessionRow` only.

## What changes

The five-row cap goes. The list becomes groups that fold on their own, one group that moves what needs you to the top, and a filter field. Titles get wider: the right edge no longer reserves 36 px for the `…` menu; on hover the menu takes the state's place (on touch it stays reserved).

## Layout, person view (default), 264 px wide

```
v Sessions                    [s] [=]     header: content search, view menu
  [ Filter sessions               ]       only with more than 5 rows, or a query
  o Needs you                       2     foreground label, brass dot; no fold, no collapse
  (PR*) Fix the relay reconnect      o    o = waiting dot
        permission prompt                 second line: the reason
  (PR*) Release notes       Failed 2h
        your Mac's login session restarted
  v You                             9
  (PR*) Ship the one session list   7:15  shimmer = working, time = idle
  (PR*) Memory recall             Done
        lightbulb · PR #4527 · checks passing     second line only for news
  (PR*) Terminal in cli (dim)     [Share] own unshared: dim, Share on hover
  (PR*) Old refactor (faded)    Ended 3h
        … 4 more
  v Aden                            6     chevron, name, count
  (AD@) Sidebar research           o      a teammate's waiting row stays in place
  (AD@) Hermes pin (faded)   Offline 2h
  v On the network                  3     below people, in every view
  (FR*) Friday inbox (dim)       Parked   no Open, Join, Resume or End
  (WI*) opencode on WSL (dim)     6:02
  > Archived 2
  (o) All sessions 18
```

The state sits on the title's line; the second line runs the full width beneath it. What needs you is never faded or dimmed. The second line carries news only: a waiting reason, a failed row's reason, or folder plus running agents and PR. Computer, repo, branch and any other reason are in the hover and in All sessions.

## States on the row

| State | Right edge | Title | Second line |
|---|---|---|---|
| busy | time | shimmer | news only |
| waiting | brass dot | | reason |
| idle | time | | news only |
| done | `Done` | | news only |
| offline | `Offline 2h` (last seen) | faded | |
| parked | `Parked` | | |
| ended | `Ended 3h` | faded | hover: reason; `lightbulb who` when it can resume |
| failed (ended) | `Failed 2h`, alert colour | faded | reason |
| network, no backend | as its state | dim, no hover fill, not a button | |

Words only where no shape says it; busy and idle keep the 2026-10-13 "no status words". Unread keeps the blue dot and finished the green dot, and either replaces the time. Waiting wins over both. One meaning each: **waiting** is live state, the session is stopped on its person. **Unread** is news since this browser last looked, and it never moves a row.

## Other states

**Zero rows.** "No one is sharing a session." and Start session, as today.

**One group** (web, working alone). No sub-header; just rows:
```
v Sessions                         [s] [=]
  (PR*) Fix login                   7:15
  (PR*) Docs                        Done
  (o) All sessions 2
```

**State view.** `Needs you`, then `Waiting · Working · Idle · Done · Parked · Offline · Ended`, empty ones dropped, then `On the network`. **Repo view.** `Needs you`, then one group per `repo`, else folder, A–Z, then `No repo`, then `On the network`. On web only folders are known, so the menu says "Repo or folder".

**Filtering.** A query matches title, folder, repo, branch, harness, person, computer, state word, waiting reason and `#PR`. It opens every group and ignores folds, so what it finds is on screen:
```
  [ waiting                     x ]
  Needs you                         2
  ...
  No sessions match "foo".  (when empty)
```
**Show** (menu): All, Mine, Live. Anything it hides is said: `23 others hidden · Show all`. Needs you ignores Show.

**Collapsed.** A collapsed group keeps its count in the header and still shows its waiting and pinned rows and the open session; `… N more` follows the same rule. Sessions collapsed whole shows a brass pill with your waiting count (same rule as the Dock) beside the live count.

## Rules

- **Needs you** holds the viewer's own `waiting` rows and failed ended rows. Failed: `engine.job.state === "failed"`, or ended because the login session restarted. Only these move; nothing ranks by `busy` (2026-10-13). A failed row leaves by Archive, by Resume, or at seven days. A waiting row cannot be archived away.
- **Inside a group:** pinned, then live newest first, then offline and ended newest first. Parked counts as live for order and is not faded.
- **Fold:** five rows per group. Expanding is remembered per browser, so `Show fewer` and a reload do not surprise anyone.
- **Never silently hidden:** every fold says N more, every Show says what it hides, All sessions counts every listed row.
- **Dock and tab badge:** the viewer's own `waiting` rows, read from rows so an unshared terminal at a prompt counts too, unless that notification kind is off.
- **Actions read `row.backend`.** A row without one has Pin and Archive (per browser), plus Share if it is the viewer's and the host wires it. Never Open, Join, Resume or End.
- **Retention:** a row joined to an engine record follows the engine; an unjoined backend row keeps today's offline rule until Step 3 retires it.
- **Persisted per browser in `sidebarPrefs`:** group-by, Show, collapsed groups, expanded groups.
- **All sessions** stays the whole list: every row with person, computer, repo and folder on its second line, and columns for state in words and time, including network rows, by day under Ended. A teammate's waiting row says Waiting, never Waiting on you.
- **Web:** only backend rows arrive, so no On the network group and no Share. Everything else reads the same. Phone is out of scope.

## Not doing

Virtualizing (decided by measurement at 80 rows; see the PR). Typed filter tokens: plain words already match state, person and PR. A Ready-for-review group, auto-resolve, PR colouring, a peek panel.
