# Teams Issue Tracker Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** A per-row ⋯ menu to close / cancel / reopen an issue from the Issues tracker page and the inbox list rows.

**Architecture:** A shared `IssueStatusMenu` (Radix `DropdownMenu`) that calls `useIssueMutations.update(id, {status})` (close→done, cancel→cancelled, reopen→todo); dropped into `IssuesPanel`'s table (an actions column) and `IssueRow`'s `desktopTrailing` slot via `InboxList`. Frontend-only — the BFF already whitelists `status`.

**Tech Stack:** React 19, `@/components/ui/dropdown-menu` (shadcn/Radix — `IssueColumns.tsx` is the reference consumer), `useIssueMutations`, `@testing-library/user-event` (Radix-in-jsdom stubs live in `tests/setup.ts`).

Conventions: frontend from `apps/frontend` (`pnpm test -- <substr>`). Per-task targeted tests; full suite/tsc/build in the final task. New tests in `src/__tests__/teams/`.

---

## Task 1: `IssueStatusMenu` component

**Files:** Create `apps/frontend/src/components/teams/issues/IssueStatusMenu.tsx`; Test `apps/frontend/src/__tests__/teams/IssueStatusMenu.test.tsx` (new).

- [ ] **Step 1: Failing test** — `src/__tests__/teams/IssueStatusMenu.test.tsx` (mirrors `ProjectPicker.test.tsx`'s `userEvent` pattern for driving a Radix menu):
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const update = vi.fn().mockResolvedValue({});
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({ update, create: vi.fn(), createChild: vi.fn(), addComment: vi.fn() }),
}));

import { IssueStatusMenu } from "@/components/teams/issues/IssueStatusMenu";

function issue(status: string) {
  return { id: "i1", title: "T", status } as never;
}

beforeEach(() => update.mockClear());

describe("IssueStatusMenu", () => {
  it("offers Close + Cancel (not Reopen) for an open issue, and closes it", async () => {
    const user = userEvent.setup();
    render(<IssueStatusMenu issue={issue("in_progress")} />);
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    expect(await screen.findByText("Close")).toBeInTheDocument();
    expect(screen.getByText("Cancel")).toBeInTheDocument();
    expect(screen.queryByText("Reopen")).toBeNull();
    await user.click(screen.getByText("Close"));
    expect(update).toHaveBeenCalledWith("i1", { status: "done" });
  });

  it("cancels an open issue", async () => {
    const user = userEvent.setup();
    render(<IssueStatusMenu issue={issue("todo")} />);
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    await user.click(await screen.findByText("Cancel"));
    expect(update).toHaveBeenCalledWith("i1", { status: "cancelled" });
  });

  it("offers only Reopen for a resolved issue, and fires onChanged", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    render(<IssueStatusMenu issue={issue("done")} onChanged={onChanged} />);
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    expect(await screen.findByText("Reopen")).toBeInTheDocument();
    expect(screen.queryByText("Close")).toBeNull();
    await user.click(screen.getByText("Reopen"));
    expect(update).toHaveBeenCalledWith("i1", { status: "todo" });
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalled());
  });
});
```

- [ ] **Step 2: Run → fail.** `pnpm test -- IssueStatusMenu`

- [ ] **Step 3: Implement** — `src/components/teams/issues/IssueStatusMenu.tsx`:
```tsx
"use client";

import { useState } from "react";
import { MoreHorizontal } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import type { Issue } from "@/components/teams/shared/types";

// Close/cancel/reopen are issue status transitions (no dedicated endpoints).
const CLOSE_STATUS = "done";
const CANCEL_STATUS = "cancelled";
const REOPEN_STATUS = "todo";

export function IssueStatusMenu({
  issue,
  onChanged,
}: {
  issue: Issue;
  onChanged?: () => void;
}) {
  const { update } = useIssueMutations();
  const [busy, setBusy] = useState(false);
  const resolved = issue.status === "done" || issue.status === "cancelled";

  async function setStatus(status: string) {
    if (busy) return;
    setBusy(true);
    try {
      await update(issue.id, { status });
      onChanged?.();
    } catch {
      // best-effort: on failure the list simply won't change
    } finally {
      setBusy(false);
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="Issue actions"
          disabled={busy}
          // The inbox row is a whole-row <Link>; prevent the trigger click from
          // navigating it (mirrors IssueRow's archive button). Radix portals the
          // menu content out, so item clicks never reach the Link.
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
          className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--mute)] hover:bg-[var(--edge)] disabled:opacity-40"
        >
          <MoreHorizontal className="h-4 w-4" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {resolved ? (
          <DropdownMenuItem onSelect={() => void setStatus(REOPEN_STATUS)}>
            Reopen
          </DropdownMenuItem>
        ) : (
          <>
            <DropdownMenuItem onSelect={() => void setStatus(CLOSE_STATUS)}>
              Close
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void setStatus(CANCEL_STATUS)}>
              Cancel
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
```

- [ ] **Step 4: Run → pass.** `pnpm test -- IssueStatusMenu`. Then `pnpm exec tsc --noEmit` (clean) + `pnpm exec eslint src/components/teams/issues/IssueStatusMenu.tsx` (clean). If `IssueUpdateInput` rejects `{ status }`, confirm via the kanban path (`KanbanBoard` already calls `onUpdateIssue(id, { status })` → `update`), so it must accept it; if tsc still complains, widen the local call with the same type the kanban uses.

- [ ] **Step 5: Commit** — `git commit -m "feat(teams): IssueStatusMenu — close/cancel/reopen dropdown (C)"`

---

## Task 2: Wire into IssuesPanel + inbox rows

**Files:** Modify `apps/frontend/src/components/teams/panels/IssuesPanel.tsx`, `apps/frontend/src/components/teams/inbox/InboxList.tsx`; Test `apps/frontend/src/__tests__/teams/IssuesPanel.test.tsx` (new, or extend if one exists). Keep existing `InboxList`/`IssueRow` tests green.

- [ ] **Step 1: Failing test** — `src/__tests__/teams/IssuesPanel.test.tsx` (asserts the actions menu renders per row):
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));
// IssueStatusMenu pulls useIssueMutations; stub it so the panel renders standalone.
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({ update: vi.fn(), create: vi.fn(), createChild: vi.fn(), addComment: vi.fn() }),
}));

import { IssuesPanel } from "@/components/teams/panels/IssuesPanel";

beforeEach(() => {
  read.mockReset();
  read.mockImplementation((path: string) => {
    if (path === "/issues")
      return { data: { issues: [{ id: "i1", title: "Fix the thing", status: "todo" }] }, isLoading: false, error: null, mutate: vi.fn() };
    return { data: { agents: [], projects: [] }, isLoading: false, error: null, mutate: vi.fn() };
  });
});

describe("IssuesPanel", () => {
  it("renders an issue-actions menu trigger in each row", () => {
    render(<IssuesPanel />);
    expect(screen.getByText("Fix the thing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /issue actions/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run → fail.** `pnpm test -- IssuesPanel`

- [ ] **Step 3: Add the actions column to `IssuesPanel.tsx`.** Add the import `import { IssueStatusMenu } from "@/components/teams/issues/IssueStatusMenu";`. In the `columns` array, add a new column BEFORE the existing `"open"` column (so ⋯ sits left of "Open →"):
```tsx
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (i) => <IssueStatusMenu issue={i} onChanged={mutate} />,
    },
```
(`mutate` is already destructured from `read<{ issues: Issue[] }>("/issues")` at the top of the component — it refreshes the Issues list after a status change, since `update()` does not invalidate `/teams/issues`.)

- [ ] **Step 4: Wire the inbox row.** In `InboxList.tsx`, add the import `import { IssueStatusMenu } from "@/components/teams/issues/IssueStatusMenu";`. At the single `<IssueRow … />` render site (around line 91), add the prop:
```tsx
                    desktopTrailing={<IssueStatusMenu issue={item.issue} />}
```
(No `onChanged` — `update()` invalidates the `/teams/inbox?…` keys, so the inbox list refreshes automatically. `IssueRow` renders `desktopTrailing` in its right-aligned desktop slot; no change to `IssueRow` itself.)

- [ ] **Step 5: Run → pass + no regression.** `pnpm test -- IssuesPanel InboxList IssueRow` (the IssuesPanel test passes; existing InboxList/IssueRow tests stay green — the added `desktopTrailing` is an optional slot). Then `pnpm exec tsc --noEmit` (clean) + `pnpm exec eslint src/components/teams/panels/IssuesPanel.tsx src/components/teams/inbox/InboxList.tsx` (clean).

- [ ] **Step 6: Commit** — `git commit -m "feat(teams): close/cancel/reopen menu on Issues page + inbox rows (C)"`

---

## Task 3: Full verification + roadmap

- [ ] **Step 1:** `pnpm exec tsc --noEmit` (clean).
- [ ] **Step 2:** `pnpm exec eslint src/components/teams/issues/IssueStatusMenu.tsx src/components/teams/panels/IssuesPanel.tsx src/components/teams/inbox/InboxList.tsx` (0 errors).
- [ ] **Step 3:** `pnpm exec vitest run` — C tests pass; only the pre-existing local-env-only files may fail (BotSetupWizard, useChatGPTOAuth, CreditsPanel, ChatGPTOAuthStep). Confirm no regression in `IssueStatusMenu`, `IssuesPanel`, `InboxList`, `IssueRow`, and the inbox band tests. (vitest may hang at teardown — read per-file results.)
- [ ] **Step 4:** `pnpm run build` — "✓ Compiled successfully" (a later Clerk-prerender failure on `/admin/health`/marketing pages is the known env-only issue, not a code error).
- [ ] **Step 5:** Roadmap: flip row C to **Done** (note the ⋯ menu shipped on both surfaces; bulk/kanban-menu deferred). Commit.

---

## Self-Review
- Spec coverage: shared `IssueStatusMenu` with contextual close/cancel/reopen (Task 1); IssuesPanel actions column with `onChanged={mutate}` + inbox `desktopTrailing` integration (Task 2); verification + roadmap (Task 3). Bulk actions, kanban-card menu, arbitrary-status picker, mobile row actions deferred (correct).
- Type consistency: `IssueStatusMenu({issue, onChanged})` used identically in both integrations; `update(id, {status})` matches `useIssueMutations`'s real signature (kanban already uses it); status constants close→done / cancel→cancelled / reopen→todo applied consistently.
- Inbox `<Link>` safety: trigger `preventDefault`+`stopPropagation` (mirrors the archive button); Radix portals content out. Verified-at-build that the menu is in the desktop trailing slot.
- Placeholder scan: none.
