# Inbox one page + Tasks absorbs Goals and Projects

Branch `feat/inbox-one-page`, 8 commits on top of `origin/main` (e774a5515).
Working tree clean. Nothing pushed.

## Status

Complete. Both parts shipped, plan doc deleted, full suite green.

## Commits

| sha | what |
|---|---|
| `a617b44af` | `shell/PanelBand.tsx` + `PanelBandEmpty` + test, exported from `shell/index.ts` |
| `766260131` | `teams-activity` aliases to `teams-inbox`; `?section` replaces `?tab` as Inbox's durable param |
| `49bfa838a` | `InboxNeedsYou` — the attention feed plus failed runs in one band |
| `effb9ef75` | `InboxHappeningNow` — live runs in Home's three-word vocabulary |
| `3858b139d` | `InboxRecently` — the activity feed, day-grouped, with `agentNames` finally wired |
| `1f3b1abba` | `InboxPage` rewritten to three bands; the New-task one-shot moves to Tasks |
| `fe7e6b814` | deletes the pill-tab machinery; prunes `inboxStorage`; updates the bundled skill |
| `1a5182eda` | Goals + Projects become Tasks groupings; `groupBy` threaded end to end; nav trimmed; plan doc deleted |

## Verification

- **Full suite:** `483 files / 3770 tests` — all pass (`NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run`).
- **`tsc --noEmit`:** clean.
- **eslint** on every changed area: 0 errors (22 warnings, all pre-existing in files not touched).
- **Brand scan:** `0 colour literal(s) outside the token layer.`

## Board-regression check — PASS

`KanbanBoard.tsx`, `KanbanCard.tsx`, `KanbanColumn.tsx`, `IssueRow.tsx` and
`IssueFiltersPopover.tsx` are **byte-identical to `origin/main`**
(`git diff origin/main -- <those five>` is empty). Nothing was moved or renamed.

Ran explicitly and green: `KanbanBoard.test.tsx` (drag/drop), `KanbanCard.test.tsx`,
`KanbanColumn.test.tsx`, `IssueRow.test.tsx`, `IssueFiltersPopover.test.tsx`,
`TasksList.test.tsx`, `TasksList.groupBy.test.tsx` — 68 tests.

Board is still the default view. A `?groupBy=` deep link opens **list** view for
that mount only (grouping is a list-view affordance and the deep link would
otherwise be invisible); the stored board-vs-list preference is never written,
and pressing the view toggle hands the view straight back. Covered by
`"hands the view back to the board when the owner asks for it"`.

## Amendments honoured

- **A** — `money/MoneySection.tsx` and the four money sections are untouched
  (`git diff origin/main -- .../panels/money/` is empty). `PanelBand` is a new
  primitive that only the Inbox bands consume; its header says Money migrates
  onto it in a follow-up.
- **B** — `companySections.ts` lost Goals, Projects **and** Activity.

## Deviations from the plan, and why

1. **`InboxList.tsx`, `SwipeToArchive.tsx` and `IssueRow.tsx` were NOT deleted.**
   The plan's delete list breaks the build: `components/landing/WorkspaceDemo.tsx`
   renders `InboxList` as marketing. They stay, with a header on `InboxList`
   saying the landing demo is now its only caller.
2. **The two `useInboxTriage` test files are not a duplicate pair.** The plan said
   to delete one. `__tests__/teams/useInboxTriage.test.tsx` covers payload
   normalization and the no-approvals rule; `.../inbox/hooks/useInboxTriage.test.ts`
   covers loading/error/ack-filtering. Both kept.
3. **The onboarding first-task prefill was carried across, not dropped.** The plan
   moved the New-task one-shot to Tasks and accepted losing the CEO-directed
   prefill on the grounds that "Tasks already owns NewIssueDialog". True for the
   *blank* dialog; false for the prefilled one, which IS the payload of Get
   Started's "Create your first task" step. `IssuesPanel` now takes
   `newTaskPrefill` and both channels (`?new=task`, `?newIssue=onboarding`) open
   the same dialog. Two tests pin it.
4. **`AttentionList`'s empty state is now a `PanelBandEmpty` line**, not a
   `PanelEmptyState` block — the state vocabulary requires one calm italic serif
   sentence with `role="status"`, and a centred 16-unit-tall empty block inside a
   band would not be that. Its only render caller was `InboxPage`.
5. **The dead-code sweep went slightly wider than named.** With the pill tabs gone,
   `shared/lib/inbox.ts`'s per-tab reads (`getRecentTouchedIssues`,
   `INBOX_MINE_ISSUE_STATUS*`), keyboard-nav projection
   (`buildInboxKeyboardNavEntries`, `resolveInboxSelectionIndex`,
   `InboxKeyboardNavEntry`) and `isMineInboxTab` had no callers, as did
   `inboxStorage`'s filter-preference store. Removed with their test blocks.
   `teamsQueryKeys.inbox.list` STAYS — `useGetStartedDerived` and
   `useIssueMutations` still use it.
6. **The plan named `src/components/control/__tests__/CompanyNav.test.tsx`; the file
   is at `src/components/chat/__tests__/CompanyNav.test.tsx`.** There is no
   `TeamsDockPanel.test.tsx` under `workspace/__tests__` matching the plan's path
   either (it exists, but the plan's step-5 command would not have found the
   others). Updated the real files.
7. **The ⌘K palette test needed no change** — it derives its count from
   `COMPANY_SECTIONS` rather than hard-coding one.

## Concerns

**1. Goal and project CREATION is now unreachable from the UI.** This is the one
thing worth a decision before merge. Aliasing `teams-goals` → `teams-issues`
means `GoalsPanel` never mounts, and it is the only surface with **New goal**,
**Edit goal**, the goal tree, and goal delete-from-list. Same for
`ProjectsListPanel` and **New project**. What survives:

- goal **detail** — reachable from the new group heading link;
- goal **edit/delete** — `GoalDetailPage` has both;
- attaching a task to an existing goal — `NewIssueDialog`'s goal picker;
- **nothing** creates a goal or a project any more, and nothing lists projects.

I implemented the alias as instructed rather than redesigning, and left both
panels + their `PANEL_LOADERS` entries in place with a comment saying they are
unreachable while the alias stands, so lifting it is a one-line restore. If the
owner expects to keep filing goals, Tasks needs a "New goal" affordance (or the
goal grouping's heading area does) before this ships.

**2. `?panel=teams-goals` on an already-open Tasks panel with a hand-changed
grouping does not re-apply.** dockview merges parameters, and the param value is
unchanged (`groupBy=goal` → `groupBy=goal`), so no prop transition fires. A plain
Tasks nav in between clears it (`clearablePanelParamKeys`) and the next Goals
click re-applies, so the loop is escapable — but a straight re-click is a no-op.
Same class of limitation the other durable params have.

**3. The bundled `lightbulb` SKILL.md text is not live on merge.** It reaches
containers only after an extended-image build and a `prod.tag` bump.

**4. `GoalDetailPage`'s breadcrumb still reads "Goals" and links to
`teamsPanelUrl("goals")`.** That now lands on Tasks grouped by goal — the right
destination — but the crumb label and the tab title no longer match word for
word. Left as-is rather than churning a detail page this task did not name.

**5. Not verified in a browser.** Everything above is tests, typecheck, lint and
the token scan. The dev walk in the plan (one tab for `?panel=teams-activity`, no
horizontal scroll at minimum dock width, a real failed run in Needs you) has not
been done.
