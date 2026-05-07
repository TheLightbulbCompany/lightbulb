# Teams Skills Atoms Port (PR 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `SkillsPanel.tsx` (61 LOC) to compose editorial atoms. Read-only surface — Skills are operator-curated.

**Architecture:** Frontend-only. The BFF surface is `GET /skills` and that's deliberate; the spec's claim that we need install/uninstall/versions/config endpoints is wrong for our model — the panel itself documents this in its supporting copy ("Skills are operator-curated. To add or modify, contact your administrator.").

**Tech Stack:** React 19, vitest, editorial atoms.

**Branch:** `plan/teams-skills-atoms-port`.

---

## File Structure

**Modify:**
- `apps/frontend/src/components/teams/panels/SkillsPanel.tsx`

**Add tests:**
- `apps/frontend/tests/unit/components/teams/panels/SkillsPanel.test.tsx`

---

## Task 0: Setup

- [ ] **Step 1: Branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git checkout main && git pull origin main
git checkout -b plan/teams-skills-atoms-port
```

- [ ] **Step 2: Commit the plan first** (PR 1/PR 2 pattern)

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add docs/superpowers/plans/2026-05-07-teams-skills-atoms-port.md
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "plan(teams): PR 5 — Skills atoms port implementation steps"
```

---

## Task 1: Refactor `SkillsPanel.tsx`

- [ ] **Step 1: Write atom-contract test**

Create `apps/frontend/tests/unit/components/teams/panels/SkillsPanel.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SkillsPanel } from "@/components/teams/panels/SkillsPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { skills: [] }, isLoading: false, error: null }),
  }),
}));

describe("SkillsPanel atom contract", () => {
  it("renders PanelMasthead with overline + h1 + supporting copy", () => {
    render(<SkillsPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /skills/i })).toBeInTheDocument();
    expect(screen.getByText(/operator-curated/i)).toBeInTheDocument();
  });

  it("renders PanelToolbar search input", () => {
    render(<SkillsPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState when no skills", () => {
    render(<SkillsPanel />);
    expect(screen.getByRole("heading", { level: 2 })).toHaveClass("font-editorial-serif");
  });
});
```

- [ ] **Step 2: Verify failing**

```sh
cd apps/frontend && pnpm test SkillsPanel -- --run
```

- [ ] **Step 3: Refactor**

Replace the panel body with:

```tsx
"use client";

import { useState, useMemo } from "react";
import { Sparkles } from "lucide-react";
import {
  PanelMasthead,
  PanelToolbar,
  PanelEmptyState,
  PanelDataTable,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Skill {
  id: string;
  name: string;
  description?: string;
  version?: string;
}

export function SkillsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ skills: Skill[] }>("/skills");
  const [search, setSearch] = useState("");

  const skills = data?.skills ?? [];
  const filtered = useMemo(
    () =>
      search
        ? skills.filter((s) =>
            (s.name + (s.description ?? "")).toLowerCase().includes(search.toLowerCase()),
          )
        : skills,
    [skills, search],
  );

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );
  }

  const columns: PanelDataColumn<Skill>[] = [
    { key: "name", header: "Name", cell: (s) => s.name, sortable: true },
    {
      key: "description",
      header: "Description",
      cell: (s) => (
        <span className="text-[var(--ink-soft)] text-sm">
          {s.description ?? "—"}
        </span>
      ),
    },
    {
      key: "version",
      header: "Version",
      align: "right",
      cell: (s) =>
        s.version ? (
          <span className="font-mono text-xs text-[var(--mute)]">v{s.version}</span>
        ) : (
          <span className="text-[var(--mute)]">—</span>
        ),
    },
  ];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Capabilities"
        title="Skills"
      />
      <p className="text-xs italic text-[var(--mute)] -mt-3 font-editorial-serif">
        Skills are operator-curated. To add or modify, contact your administrator.
      </p>
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find skill..."
        searchAriaLabel="Search skills"
      />
      {!isLoading && filtered.length === 0 ? (
        <PanelEmptyState
          icon={Sparkles}
          headline="No skills available."
          supporting="Skills extend what your agents can do. Talk to your administrator about enabling skills for your team."
        />
      ) : (
        <PanelDataTable<Skill>
          rows={filtered}
          columns={columns}
          rowKey={(s) => s.id}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 4: Verify pass**

```sh
cd apps/frontend && pnpm test SkillsPanel -- --run
```

- [ ] **Step 5: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/panels/SkillsPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/SkillsPanel.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-skills): SkillsPanel composes editorial atoms"
```

---

## Task 2: Final verify + push + open PR

- [ ] **Step 1: Full tests + lint + tsc**

```sh
cd apps/frontend && pnpm test 2>&1 | tail -10
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
```

- [ ] **Step 2: Push + PR**

```sh
git -C ... push -u origin plan/teams-skills-atoms-port
gh pr create --title "feat(teams-skills): port to editorial atom layer (PR 5 of /teams port)" --body "$(cat <<'EOF'
## Summary

Fifth PR of the multi-step /teams panel port. Refactors `SkillsPanel.tsx` (61 LOC) to compose editorial atoms. Skills remain operator-curated — no install/uninstall surface.

## What changed

- `<PanelMasthead>` with supporting copy explaining the operator-curated model
- `<PanelToolbar>` with search input
- `<PanelEmptyState>` for the no-skills case
- `<PanelDataTable>` for the list

## Out of scope

- Spec mentioned install/uninstall/versions/config — intentionally NOT added. Per panel docstring, skills are admin-curated; users don't install their own.

## Test plan

- [x] pnpm test atom-contract guards
- [x] tsc + lint clean
- [ ] Manual smoke: load /teams → Skills tab.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review notes

**Spec coverage:** atom adoption — ✅. Spec's install/uninstall claims documented as out-of-scope.

**Type consistency:** `Skill` matches the existing interface.

**Placeholder scan:** none.
