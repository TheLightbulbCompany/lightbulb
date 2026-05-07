# Teams Members Atoms Port (PR 10) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `MembersPanel.tsx` (64 LOC) to compose editorial atoms, with a Clerk-dashboard deeplink for member-management actions (we don't manage org membership in-app).

**Branch:** `plan/teams-members-atoms-port`.

---

## Audit summary

- BFF: `GET /members`. Read-only. Member management is owned by Clerk dashboard.
- Panel today: list of `email_via_clerk`/`role`/`joined_at`.
- Spec mentioned invite/role-change actions — those belong in Clerk, not here. Add a deeplink CTA to Clerk dashboard so the user can manage members in one click.

---

## Task 0: Setup

- [ ] **Step 1: Branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git checkout main && git pull origin main
git checkout -b plan/teams-members-atoms-port
```

- [ ] **Step 2: Commit the plan first** (PR 1/PR 2 pattern)

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add docs/superpowers/plans/2026-05-07-teams-members-atoms-port.md
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "plan(teams): PR 10 — Members atoms port implementation steps"
```

---

## Task 1: Refactor `MembersPanel.tsx`

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/MembersPanel.tsx`
- Test: `apps/frontend/tests/unit/components/teams/panels/MembersPanel.test.tsx`

- [ ] **Step 1: Atom-contract test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MembersPanel } from "@/components/teams/panels/MembersPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { members: [] }, isLoading: false, error: null }),
  }),
}));
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: { id: "org_test" } }),
}));

describe("MembersPanel atom contract", () => {
  it("renders PanelMasthead h1 + Clerk deeplink CTA", () => {
    render(<MembersPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /members/i })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /manage in clerk/i }),
    ).toBeInTheDocument();
  });

  it("renders PanelEmptyState when no members", () => {
    render(<MembersPanel />);
    expect(screen.getByRole("heading", { level: 2 })).toHaveClass("font-editorial-serif");
  });
});
```

- [ ] **Step 2: Refactor**

```tsx
"use client";

import { Users, ExternalLink } from "lucide-react";
import {
  PanelMasthead,
  PanelEmptyState,
  PanelDataTable,
  EditorialButton,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Member {
  id: string;
  user_id?: string;
  role?: string;
  email_via_clerk?: string;
  joined_at?: string;
}

const CLERK_DASHBOARD_URL = "https://dashboard.clerk.com";

export function MembersPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ members: Member[] }>("/members");

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );
  }

  const members = data?.members ?? [];

  const columns: PanelDataColumn<Member>[] = [
    {
      key: "email",
      header: "Member",
      cell: (m) => (
        <span className="text-[var(--ink)]">
          {m.email_via_clerk ?? m.user_id ?? m.id}
        </span>
      ),
      sortable: true,
    },
    {
      key: "role",
      header: "Role",
      cell: (m) =>
        m.role ? (
          <span className="text-xs uppercase tracking-wide text-[var(--mute)]">
            {m.role}
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "joined",
      header: "Joined",
      align: "right",
      cell: (m) =>
        m.joined_at ? (
          <span className="font-mono text-xs text-[var(--mute)]">{m.joined_at}</span>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Team"
        title="Members"
        actions={
          <a
            href={CLERK_DASHBOARD_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Manage in Clerk"
          >
            <EditorialButton variant="secondary">
              <Users className="size-4" />
              Manage in Clerk
              <ExternalLink className="size-3" />
            </EditorialButton>
          </a>
        }
      />
      <p className="text-sm text-[var(--ink-soft)] -mt-3 italic font-editorial-serif">
        Member management is handled by Clerk.
      </p>

      {!isLoading && members.length === 0 ? (
        <PanelEmptyState
          icon={Users}
          headline="No members yet."
          supporting="Invite teammates from the Clerk dashboard. They'll appear here once they accept."
        />
      ) : (
        <PanelDataTable<Member>
          rows={members}
          columns={columns}
          rowKey={(m) => m.id}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test MembersPanel -- --run
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/panels/MembersPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/MembersPanel.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-members): MembersPanel composes editorial atoms + Clerk deeplink"
```

---

## Task 2: Final verify + push + open PR

```sh
cd apps/frontend && pnpm test 2>&1 | tail -10
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
git -C ... push -u origin plan/teams-members-atoms-port
gh pr create --title "feat(teams-members): port to editorial atom layer (PR 10 of /teams port)" --body "$(cat <<'EOF'
## Summary

Final PR of the /teams panel port. Refactors `MembersPanel.tsx` (64 LOC) to compose editorial atoms with a Clerk-dashboard deeplink CTA — member management is owned by Clerk.

## What changed

- `<PanelMasthead>` with secondary CTA linking to Clerk dashboard
- `<PanelDataTable>` for the member list
- `<PanelEmptyState>` for the no-members case

## Out of scope

- In-app invite / role-change flows (intentionally — Clerk is source of truth)

## Test plan

- [x] Atom-contract guards
- [x] tsc + lint clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
