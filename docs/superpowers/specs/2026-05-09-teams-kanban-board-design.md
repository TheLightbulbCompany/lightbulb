# Teams Kanban Board Design (Sub-project #6)

**Status:** Spec
**Owner:** prasiddha
**Started:** 2026-05-09
**Roadmap row:** [`#6 Issues kanban board`](./2026-05-04-teams-ui-parity-roadmap.md)

## Background

Upstream Paperclip's `IssuesList` exposes a `viewMode: "list" | "board"` toggle that swaps the issue list for a draggable Kanban board (`paperclip/ui/src/components/KanbanBoard.tsx`, 281 LOC). Our tier-1 port shipped only the list view; the board got missed.

`/teams/inbox` was repointed to the company-wide issues feed in PR #529, so it already has the right data set for a board view. The BFF already accepts status changes via `PATCH /teams/issues/{issue_id}`. This sub-project adds the board view and the toggle.

## Goal

Port `KanbanBoard.tsx` verbatim, retheme to our editorial palette, and wire it as a list/board toggle inside `/teams/inbox`. Persist toggle state per-company in `localStorage` (matches upstream).

## Out of scope (v1)

- Sortable WITHIN a column with persisted ordering — `<SortableContext>` is wired (dnd-kit needs it for in-column drag mechanics) but drag-end only patches `status`, not position-within-column. Order within a column reflects whatever the BFF returns.
- Multi-select / bulk drag.
- Cross-tab sync of view-mode state.
- Mobile-specific layout (desktop-first; horizontal scroll on tablet is acceptable).
- New BFF endpoints. All operations use existing `PATCH /teams/issues/{id}`.

## Architecture

### Component tree

```
InboxPage
├── InboxToolbar
│   └── ViewModeToggle ⟵ NEW (segmented "List | Board" control)
└── (branch on viewMode)
    ├── InboxList ⟵ existing
    └── KanbanBoard ⟵ NEW
        ├── DndContext (@dnd-kit/core)
        │   ├── KanbanColumn × 7
        │   │   └── SortableContext
        │   │       └── KanbanCard × N
        │   └── DragOverlay
```

### Data flow

```
TeamsEventsProvider (issue.* events)
        │  invalidate /teams/issues
        ▼
useTeamsApi.read("/issues")  ───▶  KanbanBoard
                                       │ groupBy(status)
                                       ▼
                                   columnIssues
                                       │ user drags card → drop on column
                                       ▼
                              onUpdateIssue(id, { status })
                                       │
                                       ▼
                       PATCH /teams/issues/{id} { status }
                                       │
                                       ▼
                  BFF responds → events invalidate /teams/issues
                                       │
                                       ▼
                           Board re-renders with new grouping
```

No optimistic UI in v1 — relying on the realtime invalidate cycle (sub-second under normal conditions). If perceived latency is bad, follow-up PR can layer optimistic updates via SWR `mutate`.

### Files

```
apps/frontend/src/components/teams/inbox/
├── KanbanBoard.tsx              NEW    Port of paperclip/ui/src/components/KanbanBoard.tsx
├── KanbanCard.tsx               NEW    Extracted sub-component (upstream inlines it; we split for testability)
├── KanbanColumn.tsx             NEW    Extracted sub-component (same reason)
├── ViewModeToggle.tsx           NEW    Segmented control: List | Board
├── InboxToolbar.tsx             EDIT   Add ViewModeToggle to the right side of the toolbar
├── InboxPage.tsx                EDIT   Branch on viewMode → render KanbanBoard | InboxList
└── hooks/
    └── useInboxViewMode.ts      NEW    localStorage-scoped persistence

apps/frontend/src/__tests__/components/teams/inbox/
├── KanbanBoard.test.tsx         NEW
├── ViewModeToggle.test.tsx      NEW
└── hooks/
    └── useInboxViewMode.test.ts NEW
```

3-line MIT attribution header on every ported file (matches PR #3b/c/d, #4, #5 precedent).

### New npm deps

```json
{
  "@dnd-kit/core": "^6.x",
  "@dnd-kit/sortable": "^8.x",
  "@dnd-kit/utilities": "^3.x"
}
```

Pinned to whatever upstream Paperclip pins at the time of port (verify with `cat paperclip/ui/package.json`). Same major versions, no upgrades.

## Components

### `KanbanBoard`

Verbatim port of upstream. Public API:

```ts
interface KanbanBoardProps {
  issues: Issue[];
  agents?: Agent[];
  liveIssueIds?: Set<string>;
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void;
}
```

Internals:
- `boardStatuses = ["backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled"]` (verbatim from upstream).
- `columnIssues = useMemo(() => groupBy(issues, "status"), [issues])`.
- `<DndContext>` with `PointerSensor` (5px activation distance) wrapping a horizontal-scroll row of `<KanbanColumn>`s.
- `<DragOverlay>` shows the active card during drag.
- `handleDragEnd`: if the drop target is a column id (status string), patch directly; if it's a card id, look up that card's column and patch to that status. No-op if same column.

### `KanbanColumn`

- Wraps `useDroppable({ id: status })`.
- Empty columns auto-collapse to `48px` width; expand to `260px` on hover-while-dragging or when populated.
- Header: `<StatusIcon>` + label (`status.replace("_", " ").titleCase()`) + count badge.
- Body: `<SortableContext>` with `verticalListSortingStrategy` containing the cards.

### `KanbanCard`

- Wraps `useSortable({ id: issue.id })`.
- Content (verbatim): identifier mono, optional live-pulse dot (when `liveIssueIds.has(issue.id)`), title (line-clamp-2), `<PriorityIcon>`, `<Identity>` for assignee.
- Click → `Link href={`/teams/issues/${issue.identifier ?? issue.id}`}`. Click suppressed during drag via `if (isDragging) e.preventDefault()` (verbatim).

### `ViewModeToggle`

```ts
interface ViewModeToggleProps {
  value: "list" | "board";
  onChange: (value: "list" | "board") => void;
}
```

Segmented control with `Rows3` (list) and `Columns3` (board) icons. Existing button styles, no new shadcn additions.

### `useInboxViewMode`

```ts
function useInboxViewMode(companyId: string | null): {
  viewMode: "list" | "board";
  setViewMode: (mode: "list" | "board") => void;
};
```

`localStorage` key shape `isol8:teams:inbox-view-mode:{companyId}`. Defaults to `"list"`. Hydration-safe (null company yields `"list"`, no storage read until companyId resolves).

## Editorial retheme overlay

The only deviation from a verbatim port. Rules consistent with the org-chart, IssueColumns, and other recent ports:

| Upstream | Isol8 |
|---|---|
| `bg-card` | `editorial-card` (existing utility class) |
| `bg-muted/20` (column body) | `var(--paper-soft)` |
| `bg-accent/40` (column drop hover) | `var(--paper)` with `var(--ink)` 5% overlay |
| `text-muted-foreground` | `var(--mute)` |
| `bg-blue-400/500` (live dot, animate-pulse) | `var(--forest)` (matches our running-status dot) |
| `ring-primary/20` (drag overlay) | `var(--ink)` low-alpha (`ring-[color:var(--ink)] ring-opacity-20`) |
| Default `font-sans` headers | `font-editorial-sans` for column headers, body uses inherited stack |
| `bg-violet-500` (in_review status color in `IssuesList`) | re-route through `<StatusIcon>`'s editorial color map (T16) |

`<StatusIcon>` and `<PriorityIcon>` already exist in `apps/frontend/src/components/teams/shared/` and were retheme'd in T16. Reuse as-is. `<Identity>` exists at `components/teams/shared/Identity.tsx`.

No retheme of dnd-kit's drag-state CSS (transforms / transitions are mechanical, not visual chrome).

## Realtime

Already wired via `TeamsEventsProvider` (PR #518). `issue.*` events invalidate `/teams/issues` SWR cache; `KanbanBoard` reads the same cache, so a status change committed by anyone (including the local user via drag) causes a re-fetch and re-grouping.

`liveIssueIds` is the existing `Set<string>` already maintained in `TeamsEventsProvider`'s state — the same set the InboxList already uses for its row blue-pulse indicator. Pass it through unchanged.

## Error handling

| Failure | Behavior |
|---|---|
| `PATCH /teams/issues/{id}` rejects (4xx/5xx) | Card snaps back to source column when SWR refetches `/teams/issues` (status was never optimistically updated locally). Surface a notification via the existing notification surface: "Couldn't update status — please retry." |
| BFF returns issues with unknown `status` (not in `boardStatuses`) | Card is dropped silently. Console warn in dev. (Upstream behavior — no special-casing.) |
| `companyId` not yet resolved | `useInboxViewMode` returns `"list"` default; toggle disabled. |
| dnd-kit fails to load (split chunk) | Suspense fallback → InboxList renders; toggle hidden until chunk lands. Acceptable for v1. |

## Testing

Vitest unit tests, no E2E in v1.

| Suite | Cases |
|---|---|
| `KanbanBoard.test.tsx` | (1) groups N issues into 7 columns by status; (2) drag-end on column id calls `onUpdateIssue` with new status; (3) drag-end on card id resolves the card's column status; (4) same-column drop is a no-op (no patch call); (5) renders empty-column collapsed state; (6) renders count badge per column; (7) cards with unknown status are not rendered. |
| `ViewModeToggle.test.tsx` | (1) renders both buttons; (2) calls onChange with the opposite value on click; (3) active button has aria-pressed=true. |
| `useInboxViewMode.test.ts` | (1) defaults to "list" with no stored value; (2) reads stored value; (3) writes on setViewMode; (4) scopes per companyId; (5) returns "list" with null companyId without touching localStorage. |

Skip dnd-kit gesture simulation — well-tested upstream and by the lib itself. Cover wiring (group-by, patch-on-end) and the toggle/persistence layer only.

## Roadmap update

Add row #6 to `docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md`:

| # | Sub-project | Effort | Status | Spec | Plan | PR |
|---|---|---|---|---|---|---|
| 6 | Issues kanban board (list/board toggle in /teams/inbox; verbatim KanbanBoard port + retheme) | M | Spec | [2026-05-09-teams-kanban-board-design.md](./2026-05-09-teams-kanban-board-design.md) | — | — |

## Open questions resolved by directive

- **Surface placement** — toggle inside `/teams/inbox` (matches upstream's `IssuesList`).
- **DnD library** — `@dnd-kit/*` verbatim, not a native HTML5 rewrite.
- **Sortable within column** — wired via upstream's `<SortableContext>` but drag-end only patches status, not order.

## Risks

- **dnd-kit bundle size** — three new packages, ~50KB gzipped. Lazy-load behind the board toggle to keep the list-view path unchanged. Done via `dynamic(() => import("./KanbanBoard"))`.
- **Realtime invalidate latency** — if `issue.*` events lag, the dragged card may visually snap back briefly. Mitigation deferred to a follow-up optimistic-update PR if observed.
- **Status enum drift** — if the BFF starts emitting a status not in `boardStatuses`, those issues silently disappear from the board. Mitigation: assert the enum in `KanbanBoard` tests and fail fast if upstream Paperclip adds a new value.
