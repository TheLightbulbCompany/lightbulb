Fourth PR of the owner-tabs consolidation (spec: `docs/superpowers/specs/2026-09-06-owner-tabs-consolidation-design.md`). Follows #2343 (Home), #2351 (Money) and #2368 (rail cut + the Money chart). Two halves that only make sense together, so they ship together.

## Inbox and Tasks were the same page wearing two hats

Inbox was five pill tabs — mine · unread · recent · all — over the **same issue rows** the Tasks page renders, plus one tab that was the attention feed. Two sidebar items, two mental models, one dataset.

They now split by **kind**, not by filter:

- **Inbox answers "what needs me."** Three bands: **Needs you** (approvals with Approve/Decline in place, agent questions answered inline, blocked items, and failed runs with Retry/Dismiss), **Happening now** (live runs, in Home's exact three words: `queued` · `running` · `stalled`), **Recently** (the activity feed, grouped by day, one type filter). No task rows anywhere on it.
- **Tasks answers "what is the work."** Every issue lives here, and it keeps the board as its default view.

The pill tabs, the toolbar, the needs-attention band, the issue list, the keyboard-nav layer, the archive stack and the per-browser read marks are all deleted. `teams-activity` aliases to `teams-inbox` and lands on Recently.

**Failed runs deliberately move into "Needs you."** The attention feed excludes them on the argument that they "keep their own home in the Agent failures band" — and this PR deletes that band. Relying on the feed alone would have silently dropped failed runs from the owner's view, so both feeds render under one label. They stay separate data paths; only the presentation merges.

## Goals and Projects become groupings of Tasks

Tasks already grouped by status, assignee, priority, project and parent, and an issue already carried its goal id — so this is mostly wiring. A `goal` grouping joins them, each heading showing "N of M done" and **linking to that goal's own page**, so a goal's description, status, owner and parent survive losing its tab. `teams-goals` and `teams-projects` alias to `teams-issues` with the grouping synthesized from the raw key, the same mechanism the four Money keys use.

**A regression this caught, and fixed.** Absorbing those two tabs means their panels never mount — and each held the *only* surface that could CREATE a goal or a project, as a private function inside it. Viewing, editing and deleting survive behind the heading link; creation would have vanished. Both dialogs are extracted (`goals/CreateGoalDialog.tsx`, `issues/CreateProjectDialog.tsx`) and Tasks offers **New goal** / **New project** while grouped that way, where the action means something.

## The board is untouched

`KanbanBoard`, `KanbanCard`, `KanbanColumn`, `IssueRow` and `IssueFiltersPopover` are byte-identical to `main` — verified as an empty diff, not asserted. Board stays the default view, its per-browser preference is unchanged, and a `?groupBy=` deep link scopes that mount only and never writes the stored preference. Their suites plus both Tasks suites were run explicitly.

## Three places the plan was wrong, corrected during the build

- `InboxList`, `SwipeToArchive` and `IssueRow` **cannot** be deleted: the marketing landing page's `WorkspaceDemo` renders them.
- The two `useInboxTriage` test files are not duplicates — they cover different cases. Both kept.
- The onboarding "create your first task" **prefill** moved to Tasks rather than being dropped; it is the payload of Get Started's first step, not decoration.

## The sidebar

22 items → 14 in #2368 → **11 here** (Goals, Projects and Activity go). Search folding into ⌘K and Org chart moving into Agents closes it to the nine the spec calls for. Every retired key stays registered, so saved links, bookmarks and agent-built URLs still resolve.

## Verify

Full suite green on the branch · `tsc --noEmit` clean · `brand-tokens-scan` prints 0 · eslint clean on changed files.

**Dev walk owed after merge:** Inbox shows three bands and no task rows; `?panel=teams-activity` opens ONE Inbox tab at Recently; `?panel=teams-goals` and `?panel=teams-projects` each open ONE Tasks tab pre-grouped; the board still renders and drags and is still the default; New goal and New project both work; a failed run shows with Retry and does not sit under "nothing needs you"; Recently groups under Today and names agents rather than raw ids.

⚠️ The bundled `lightbulb` skill's Inbox section is updated here but reaches containers only after an image build and a `prod.tag` bump.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
