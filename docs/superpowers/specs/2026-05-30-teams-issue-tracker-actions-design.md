# Teams Issue Tracker Actions — Design

**Roadmap:** sub-project **C** (Issue tracker actions). Extends #6 (issue pickers).

**Status:** Spec

## Goal

Let a `/teams` user **close / cancel / reopen** an issue directly from a list row — on the standalone **Issues** tracker page and the **inbox list** view — via a per-row ⋯ menu, without opening the detail page or switching to the board. Frontend-only.

## Background

- Upstream issue statuses: `backlog · todo · in_progress · in_review · done · blocked · cancelled`. Close/cancel/reopen are **status transitions** (close → `done`, cancel → `cancelled`, reopen → `todo`) via the generic `PATCH /issues/:id { status }` — there are no dedicated close/cancel/reopen endpoints. (Archive is a *separate* action, already shipped in sub-project E.)
- Isol8 already changes issue status via the **kanban board** (drag between columns) and the **detail** page. The BFF `PatchIssueBody` already whitelists `status`, and `useIssueMutations.update(id, {status})` already `PATCH`es it (revalidating the detail key + the inbox lists `/teams/inbox?…`).
- **The gap:** flat list rows can't change status. The standalone Issues page (`IssuesPanel`, a `PanelDataTable`) rows only link to detail; the inbox list rows (`IssueRow` via `InboxList`) only navigate. There is **no standalone-vs-inbox decision to make** — Isol8 already has both an Inbox and an Issues page; C just adds row actions to both.

## Decisions

1. **One shared `IssueStatusMenu`** — a ⋯ `DropdownMenu` (the existing `@/components/ui/dropdown-menu`; `IssueColumns.tsx` is the proven consumer to mirror). Props: `{ issue: Issue; onChanged?: () => void }`. Used by both surfaces.
2. **Contextual items by current status:**
   - **Open** (`backlog`/`todo`/`in_progress`/`in_review`/`blocked`) → **Close** (→`done`) and **Cancel** (→`cancelled`).
   - **Resolved** (`done`/`cancelled`) → **Reopen** (→`todo`).
   (An issue is "resolved" iff `status ∈ {done, cancelled}`; everything else is "open".)
3. **Action = a status PATCH.** On select: `await useIssueMutations().update(issue.id, { status }); onChanged?.()`.
4. **Inbox `<Link>` safety.** The menu trigger calls `e.preventDefault()` + `e.stopPropagation()` so a click opens the menu instead of navigating the row `<Link>` — mirroring `IssueRow`'s existing archive (✕) button. Radix portals the menu content to `<body>`, so item clicks never reach the `<Link>`.
5. **Revalidation.** `update()` already invalidates the detail key + inbox lists, so the **inbox auto-refreshes**. The Issues page reads `/issues` (key `/teams/issues`), which `update()` does NOT invalidate, so **`IssuesPanel` passes its own `mutate` as `onChanged`**.
6. **No backend change.** Kanban (drag) and the detail picker are untouched.

## Architecture

### New: `issues/IssueStatusMenu.tsx`
- `IssueStatusMenu({ issue, onChanged }: { issue: Issue; onChanged?: () => void })`.
- Computes `resolved = issue.status === "done" || issue.status === "cancelled"`.
- Renders `<DropdownMenu>` → `<DropdownMenuTrigger>` (a small ⋯ icon button that `preventDefault`+`stopPropagation`s) → `<DropdownMenuContent>` with the contextual `<DropdownMenuItem>`s (Close/Cancel, or Reopen).
- Each item's `onSelect` (or onClick) runs the status PATCH via `useIssueMutations().update` and then `onChanged?.()`. A local `busy` guard prevents double-fire; on error, surfaces nothing destructive (best-effort; the row simply won't change).

### Integration A — `panels/IssuesPanel.tsx`
- Add a right-aligned `actions` column to the `PanelDataTable` columns: `cell: (i) => <IssueStatusMenu issue={i} onChanged={mutate} />` (`mutate` from the panel's existing `read<{issues}>("/issues")`). Keep the existing "Open →" column.

### Integration B — `inbox/InboxList.tsx`
- At the single `<IssueRow … />` render site, add `desktopTrailing={<IssueStatusMenu issue={item.issue} />}`. `IssueRow` renders `desktopTrailing` in its right-aligned, desktop-only trailing slot. (No `onChanged` needed — `update()` invalidates `/teams/inbox?…` so the list refreshes.) No change to the ported `IssueRow` itself.

### Status vocabulary
A module-level constant in `IssueStatusMenu` — `CLOSE = "done"`, `CANCEL = "cancelled"`, `REOPEN = "todo"` — labelled actions, not a free status picker.

## Out of scope (deferred)
- Bulk / multi-select status actions.
- An explicit menu on **kanban cards** (drag already covers the board).
- A general status picker (only the three labelled transitions).
- Transitions into `in_review`/`blocked`/`backlog` from the row (those stay in the detail picker / board).
- Mobile inbox row actions (the `desktopTrailing` slot is desktop-only; mobile uses detail). 

## Testing
- **`IssueStatusMenu`:** an open issue's menu shows **Close + Cancel** (no Reopen); a `done`/`cancelled` issue's menu shows **Reopen** only; selecting **Close** calls `update(id, {status:"done"})` and then `onChanged`; selecting **Reopen** calls `update(id, {status:"todo"})`. (Mock `useIssueMutations`.)
- **`IssuesPanel`:** the actions column renders an `IssueStatusMenu` per row (trigger present).
- **Inbox:** `InboxList` passes the menu into `IssueRow`'s `desktopTrailing`; the trigger click does not navigate (preventDefault/stopPropagation) — assert via a spy/stopPropagation check or that the menu opens without a route change.

## Convention
Plan: `docs/superpowers/plans/2026-05-30-teams-issue-tracker-actions.md`.
