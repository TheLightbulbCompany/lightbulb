# Onboarding First-Task Pre-fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Get Started "Create your first task" step open a pre-filled, editable New Task dialog assigned to Friday (CEO) instead of a blank one.

**Architecture:** Three frontend changes. A new shared module holds the editable default copy + a resolver for the seeded CEO. `NewIssueDialog` gains optional `defaultTitle`/`defaultDescription`/`defaultAssigneeAgentId` props that seed both its initial state and its reset baseline (mirroring the existing `defaultProjectId`). The Get Started task handoff signals the onboarding variant via `?newIssue=onboarding`; `InboxPage` captures that intent as state (so it survives the param-strip and re-opens) and passes the defaults down. The plain `?newIssue=1` toolbar path stays blank.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript, Tailwind v4, lucide-react, vitest + @testing-library/react.

## Global Constraints

- **Frontend only.** No backend endpoints, no DynamoDB, no new npm deps.
- **Spec:** `docs/superpowers/specs/2026-06-26-onboarding-first-task-prefill-design.md`.
- **Default copy (verbatim, option C, CEO-framed):**
  - Title: `Build out my team for [your goal]`
  - Description (multi-line, see Task 1).
- **Friday resolution:** seeded CEO only — `seeded === true && role === "ceo"`, then seeded agent named "Friday"; never a user-created look-alike. `seeded` is server-stamped.
- **Deep-link contract:** `newIssue=onboarding` = pre-filled; `newIssue=1` = blank (unchanged).
- **Testing:** vitest, **TARGETED single-file runs only** (`pnpm vitest run <file>`). NEVER run the full `pnpm test` suite (~17.7 GB RAM). CI runs `tsc --noEmit` over test files too — annotate mock-builder return types.
- **Copy/comment rule:** comments explain WHY, never reference PR numbers or this plan.

---

### Task 1: Shared first-task copy + CEO resolver

**Files:**
- Create: `apps/frontend/src/components/getstarted/getStartedFirstTask.ts`
- Test: `apps/frontend/src/components/getstarted/__tests__/getStartedFirstTask.test.ts`

**Interfaces:**
- Produces: `ONBOARDING_FIRST_TASK_TITLE: string`, `ONBOARDING_FIRST_TASK_DESCRIPTION: string`, `resolveCeoAgentId(agents: CompanyAgent[]): string | undefined`.
- Consumes: `CompanyAgent` from `@/components/teams/shared/types`.

- [ ] **Step 1: Write the failing test**

```typescript
// apps/frontend/src/components/getstarted/__tests__/getStartedFirstTask.test.ts
import { describe, it, expect } from "vitest";
import type { CompanyAgent } from "@/components/teams/shared/types";
import {
  ONBOARDING_FIRST_TASK_TITLE,
  ONBOARDING_FIRST_TASK_DESCRIPTION,
  resolveCeoAgentId,
} from "../getStartedFirstTask";

function agent(over: Partial<CompanyAgent> = {}): CompanyAgent {
  return { id: "ag", name: "Agent", ...over };
}

describe("getStartedFirstTask", () => {
  it("copy is non-empty and carries the replace-me token", () => {
    expect(ONBOARDING_FIRST_TASK_TITLE).toContain("[your goal]");
    expect(ONBOARDING_FIRST_TASK_DESCRIPTION).toContain("[your goal]");
    expect(ONBOARDING_FIRST_TASK_DESCRIPTION.length).toBeGreaterThan(20);
  });

  it("resolves the seeded CEO by role", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "friday", name: "Friday", role: "ceo", seeded: true }),
      agent({ id: "atlas", name: "Atlas", role: "general", seeded: true }),
    ];
    expect(resolveCeoAgentId(agents)).toBe("friday");
  });

  it("falls back to a seeded agent named Friday when role is absent", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "friday", name: "Friday", seeded: true }),
    ];
    expect(resolveCeoAgentId(agents)).toBe("friday");
  });

  it("ignores a user-created agent named Friday / using role ceo", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "imposter", name: "Friday", role: "ceo", seeded: false }),
    ];
    expect(resolveCeoAgentId(agents)).toBeUndefined();
  });

  it("returns undefined when no seeded CEO exists", () => {
    expect(resolveCeoAgentId([])).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/getStartedFirstTask.test.ts`
Expected: FAIL — cannot find module `../getStartedFirstTask`.

- [ ] **Step 3: Write the implementation**

```typescript
// apps/frontend/src/components/getstarted/getStartedFirstTask.ts
import type { CompanyAgent } from "@/components/teams/shared/types";

// Pre-filled scaffold for the Get Started "Create your first task" step. The
// user edits it before hitting Create — the point is a populated, CEO-directed
// first task instead of a blank composer. "[your goal]" is an obvious
// replace-me token left in the editable fields.
export const ONBOARDING_FIRST_TASK_TITLE = "Build out my team for [your goal]";

export const ONBOARDING_FIRST_TASK_DESCRIPTION = `You are my CEO. Help me stand up a team to deliver [your goal].

- Work out what roles and skills this goal needs.
- Break it into concrete tasks and start delegating.
- If a needed skill isn't on the team yet, bring it in.
- Report back with the plan and the first tasks you've kicked off.`;

// Resolve the seeded CEO (Friday) so the onboarding first task is assigned to
// the orchestrator. `seeded` is server-stamped from the persisted seeded-agent
// IDs (never inferred from role/name), so a user-created agent named "Friday"
// or using role "ceo" is never matched. Returns undefined when no seeded CEO is
// present (e.g. roster not loaded yet) — the dialog then opens unassigned.
export function resolveCeoAgentId(agents: CompanyAgent[]): string | undefined {
  const byRole = agents.find((a) => a.seeded === true && a.role === "ceo");
  if (byRole) return byRole.id;
  const byName = agents.find(
    (a) => a.seeded === true && a.name.toLowerCase() === "friday",
  );
  return byName?.id;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/getStartedFirstTask.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/getstarted/getStartedFirstTask.ts apps/frontend/src/components/getstarted/__tests__/getStartedFirstTask.test.ts
git commit -m "feat(getstarted): onboarding first-task copy + CEO resolver"
```

---

### Task 2: `NewIssueDialog` seedable default props

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/issues/NewIssueDialog.test.tsx` (add cases)

**Interfaces:**
- Produces: `NewIssueDialogProps` gains `defaultTitle?: string`, `defaultDescription?: string`, `defaultAssigneeAgentId?: string`. Each seeds the initial state AND the `reset()` baseline. Omitted = blank (no behavior change).

- [ ] **Step 1: Write the failing test** (add to the existing describe block)

```tsx
// add inside describe("NewIssueDialog", …) in NewIssueDialog.test.tsx
test("seeds Title/Description/assignee from default props", () => {
  const agents: CompanyAgent[] = [
    { id: "friday", name: "Friday", role: "ceo", seeded: true },
  ];
  render(
    <NewIssueDialog
      {...baseProps}
      agents={agents}
      defaultTitle="Build out my team for [your goal]"
      defaultDescription="You are my CEO."
      defaultAssigneeAgentId="friday"
    />,
  );
  expect(screen.getByPlaceholderText("Task title")).toHaveValue(
    "Build out my team for [your goal]",
  );
  expect(screen.getByPlaceholderText("Optional details...")).toHaveValue(
    "You are my CEO.",
  );
  expect(screen.getByLabelText(/assign to agent/i)).toHaveValue("friday");
});

test("stays blank when no default props are given", () => {
  render(<NewIssueDialog {...baseProps} />);
  expect(screen.getByPlaceholderText("Task title")).toHaveValue("");
  expect(screen.getByPlaceholderText("Optional details...")).toHaveValue("");
});
```

(`CompanyAgent` is already imported at the top of this test file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/components/teams/issues/NewIssueDialog.test.tsx`
Expected: FAIL — Title renders empty (props not wired yet).

- [ ] **Step 3: Add the props to the interface**

In `NewIssueDialog.tsx`, in `NewIssueDialogProps`, immediately after the `defaultProjectId?: string;` member (the `defaultProjectId` JSDoc + line), add:

```typescript
  /** Pre-fills the Title field and its reset baseline. */
  defaultTitle?: string;
  /** Pre-fills the Description field and its reset baseline. */
  defaultDescription?: string;
  /** Pre-selects the assignee and its reset baseline. */
  defaultAssigneeAgentId?: string;
```

- [ ] **Step 4: Destructure the new props**

In the component signature, after the `defaultProjectId,` line, add:

```typescript
  defaultTitle,
  defaultDescription,
  defaultAssigneeAgentId,
```

- [ ] **Step 5: Seed initial state from the props**

Replace the three relevant `useState` initializers:

```typescript
  const [title, setTitle] = useState(defaultTitle ?? "");
  const [description, setDescription] = useState(defaultDescription ?? "");
```
and
```typescript
  const [assigneeAgentId, setAssigneeAgentId] = useState<string>(
    defaultAssigneeAgentId ?? "",
  );
```

(Leave `priority`/`status`/`workMode`/`projectId` exactly as they are.)

- [ ] **Step 6: Seed the reset baseline too**

In `reset()`, replace the corresponding setters:

```typescript
    setTitle(defaultTitle ?? "");
    setDescription(defaultDescription ?? "");
```
and
```typescript
    setAssigneeAgentId(defaultAssigneeAgentId ?? "");
```

(`setProjectId(defaultProjectId ?? "")` stays as-is.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/components/teams/issues/NewIssueDialog.test.tsx`
Expected: PASS (all existing tests + the 2 new ones).

- [ ] **Step 8: Type-check + commit**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors.

```bash
git add apps/frontend/src/components/teams/issues/NewIssueDialog.tsx apps/frontend/src/__tests__/components/teams/issues/NewIssueDialog.test.tsx
git commit -m "feat(teams): NewIssueDialog accepts seedable default title/description/assignee"
```

---

### Task 3: Onboarding deep-link wiring (handoff + InboxPage pre-fill)

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx:801`
- Modify: `apps/frontend/src/components/teams/inbox/InboxPage.tsx` (param region `:149-162`, dialog render `:372-381`, imports)
- Test: `apps/frontend/src/__tests__/components/teams/inbox/InboxPage.prefill.test.tsx`

**Interfaces:**
- Consumes: `ONBOARDING_FIRST_TASK_TITLE`, `ONBOARDING_FIRST_TASK_DESCRIPTION`, `resolveCeoAgentId` (Task 1); `NewIssueDialog` default props (Task 2).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/components/teams/inbox/InboxPage.prefill.test.tsx
import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { CompanyAgent, Issue } from "@/components/teams/shared/types";

vi.mock("@/components/teams/inbox/hooks/useInboxData", () => ({
  useInboxData: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useInboxKeyboardNav", () => ({
  useInboxKeyboardNav: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useInboxArchiveStack", () => ({
  useInboxArchiveStack: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useReadInboxItems", () => ({
  useReadInboxItems: vi.fn(),
}));
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: vi.fn(),
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: undefined,
      isLoading: false,
      error: undefined,
      isValidating: false,
      mutate: vi.fn(),
    }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams("newIssue=onboarding"),
}));

import { InboxPage } from "@/components/teams/inbox/InboxPage";
import { useInboxData } from "@/components/teams/inbox/hooks/useInboxData";
import { useInboxKeyboardNav } from "@/components/teams/inbox/hooks/useInboxKeyboardNav";
import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";

const agents: CompanyAgent[] = [
  { id: "friday", name: "Friday", role: "ceo", seeded: true },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useInboxData).mockReturnValue({
    mineIssues: [] as Issue[],
    touchedIssues: [] as Issue[],
    allIssues: [] as Issue[],
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof useInboxData>);
  vi.mocked(useInboxKeyboardNav).mockReturnValue(
    undefined as unknown as ReturnType<typeof useInboxKeyboardNav>,
  );
  vi.mocked(useInboxArchiveStack).mockReturnValue({
    archive: vi.fn(),
    markRead: vi.fn(),
    archivingIssueIds: new Set<string>(),
  } as unknown as ReturnType<typeof useInboxArchiveStack>);
  vi.mocked(useReadInboxItems).mockReturnValue({
    markRead: vi.fn(),
    isRead: () => false,
  } as unknown as ReturnType<typeof useReadInboxItems>);
  vi.mocked(useIssueMutations).mockReturnValue({
    create: vi.fn().mockResolvedValue({ id: "iss_new" }),
    createChild: vi.fn(),
    update: vi.fn(),
    addComment: vi.fn(),
    remove: vi.fn(),
    monitorCheckNow: vi.fn(),
  } as unknown as ReturnType<typeof useIssueMutations>);
});

describe("InboxPage onboarding pre-fill", () => {
  test("?newIssue=onboarding opens the dialog pre-filled with the CEO task", () => {
    render(
      <InboxPage companyId="co_1" currentUserId="u_1" agents={agents} />,
    );
    expect(screen.getByPlaceholderText("Task title")).toHaveValue(
      "Build out my team for [your goal]",
    );
    expect(screen.getByLabelText(/assign to agent/i)).toHaveValue("friday");
  });
});
```

> If `InboxPage`'s required prop set differs from `{ companyId, currentUserId, agents }`, match its real signature (see `apps/frontend/src/__tests__/components/teams/inbox/InboxPage.test.tsx` `baseProps`) — keep `agents` set to the `friday` roster above.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/components/teams/inbox/InboxPage.prefill.test.tsx`
Expected: FAIL — Title renders empty (`onboarding` not yet recognized / defaults not passed).

- [ ] **Step 3: Add the import to `InboxPage.tsx`**

Near the other `@/components/...` imports, add:

```typescript
import {
  ONBOARDING_FIRST_TASK_TITLE,
  ONBOARDING_FIRST_TASK_DESCRIPTION,
  resolveCeoAgentId,
} from "@/components/getstarted/getStartedFirstTask";
```

- [ ] **Step 4: Recognize the `onboarding` variant + capture prefill as state**

Replace the existing param block (`InboxPage.tsx:149-162`) with:

```typescript
  const newIssueParam = searchParams.get("newIssue");
  // Deep-link open: "1" = blank composer (toolbar / chat pill); "onboarding" =
  // the Get Started first-task step, which pre-fills a CEO-directed task. The
  // param is stripped right after open (effect below), so the prefill intent is
  // captured as STATE — not re-derived from the param — to survive the strip
  // and any later same-page re-open.
  const [newIssueOpen, setNewIssueOpen] = useState(
    () => newIssueParam === "1" || newIssueParam === "onboarding",
  );
  const [newIssuePrefill, setNewIssuePrefill] = useState(
    () => newIssueParam === "onboarding",
  );
  const [prevNewIssueParam, setPrevNewIssueParam] = useState(newIssueParam);
  if (newIssueParam !== prevNewIssueParam) {
    setPrevNewIssueParam(newIssueParam);
    if (newIssueParam === "1" || newIssueParam === "onboarding") {
      setNewIssueOpen(true);
      setNewIssuePrefill(newIssueParam === "onboarding");
    }
  }

  useEffect(() => {
    if (newIssueParam !== "1" && newIssueParam !== "onboarding") return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("newIssue");
    router.replace(`?${params.toString()}`, { scroll: false });
  }, [newIssueParam, searchParams, router]);
```

- [ ] **Step 5: Pass the defaults to the dialog**

Replace the `<NewIssueDialog … />` render (`InboxPage.tsx:372-381`) with:

```tsx
      <NewIssueDialog
        key={newIssuePrefill ? "onboarding" : "blank"}
        open={newIssueOpen}
        onOpenChange={(next) => {
          setNewIssueOpen(next);
          if (!next) setNewIssuePrefill(false);
        }}
        agents={agents}
        projects={projects}
        defaultTitle={newIssuePrefill ? ONBOARDING_FIRST_TASK_TITLE : undefined}
        defaultDescription={
          newIssuePrefill ? ONBOARDING_FIRST_TASK_DESCRIPTION : undefined
        }
        defaultAssigneeAgentId={
          newIssuePrefill ? resolveCeoAgentId(agents) : undefined
        }
        onCreated={(issueId) => {
          setNewIssueOpen(false);
          setNewIssuePrefill(false);
          openIssue(issueId);
        }}
      />
```

The `key` forces a fresh mount (re-seed) when the prefill intent flips on, since the dialog is otherwise permanently mounted and `useState` would not re-run. `newIssuePrefill` is state (not the strippable param), so the key is stable while the dialog is open.

- [ ] **Step 6: Flip the Get Started handoff to the onboarding variant**

In `ChatLayout.tsx`, in the `task:` action, change the one line (`:801`):

```typescript
                  params.set("newIssue", "onboarding");
```

(Everything else in that action — panel, view, dropped agent/detailId, the single `router.push("/chat?…")` — is unchanged.)

- [ ] **Step 7: Run the prefill test + tsc**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/components/teams/inbox/InboxPage.prefill.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 8: Commit**

```bash
git add apps/frontend/src/components/chat/ChatLayout.tsx apps/frontend/src/components/teams/inbox/InboxPage.tsx apps/frontend/src/__tests__/components/teams/inbox/InboxPage.prefill.test.tsx
git commit -m "feat(getstarted): pre-fill the first-task step with a CEO-assigned task"
```

---

### Task 4: Regression guard + final verification

**Files:**
- Verify only (no new code unless a regression surfaces).

- [ ] **Step 1: Run all three touched test files (targeted, never the full suite)**

Run:
```bash
cd apps/frontend && pnpm vitest run \
  src/components/getstarted/__tests__/getStartedFirstTask.test.ts \
  src/__tests__/components/teams/issues/NewIssueDialog.test.tsx \
  src/__tests__/components/teams/inbox/InboxPage.test.tsx \
  src/__tests__/components/teams/inbox/InboxPage.prefill.test.tsx
```
Expected: all PASS — including the pre-existing `InboxPage.test.tsx` (its `?newIssue` default is empty, so it must still render blank with no regression).

- [ ] **Step 2: Type-check + lint the changed files**

Run: `cd apps/frontend && pnpm exec tsc --noEmit && pnpm run lint`
Expected: no errors in the modified files.

- [ ] **Step 3: Manual behavior verification (dev) — run-trigger note**

After deploy to dev: sign in as a fresh owner, click the Get Started "New task" step, confirm the dialog opens pre-filled (title + CEO brief, assignee = Friday), edit and Create, and confirm you land on the task. **Observe whether Friday auto-starts a run on this `todo` task** (upstream treats assignment as a trigger). Record the actual behavior in the PR description — do NOT block the PR on it; it's informational. If a run fires and is undesirable, that's a separate follow-up (e.g. seed as `backlog`), not part of this change.

- [ ] **Step 4: No-op commit guard**

If Steps 1-2 surfaced a fix, commit it:

```bash
git add -A
git commit -m "fix(getstarted): address verification findings"
```

Otherwise nothing to commit.

---

## Self-Review

**Spec coverage:**
- `NewIssueDialog` seedable defaults (title/description/assignee) → Task 2. ✅
- Shared copy constant + `resolveCeoAgentId` (seeded-CEO only) → Task 1. ✅
- Deep-link `newIssue=onboarding` from handoff → Task 3 Step 6. ✅
- `InboxPage` recognizes onboarding, survives param-strip + re-open (state + key) → Task 3 Steps 4-5. ✅
- Blank toolbar path (`newIssue=1`) unchanged → Task 3 (both branches) + Task 4 Step 1 regression. ✅
- Default copy = approved option C, CEO-framed → Task 1. ✅
- Run-trigger verified, not forced → Task 4 Step 3. ✅
- Frontend-only, no new deps → all tasks. ✅

**Placeholder scan:** No TBD/TODO. The `[your goal]` token is intentional editable copy, asserted by Task 1's test. The one conditional note (Task 3 Step 1) names the exact fallback (match `InboxPage.test.tsx` `baseProps`) — not a blind placeholder.

**Type consistency:** `resolveCeoAgentId(agents: CompanyAgent[]): string | undefined` defined in Task 1, consumed unchanged in Task 3. `defaultTitle`/`defaultDescription`/`defaultAssigneeAgentId` named identically across Tasks 2 and 3. `ONBOARDING_FIRST_TASK_TITLE`/`_DESCRIPTION` named identically across Tasks 1, 3, and the tests.

## Notes for the implementer

- Build in order: Task 1 (zero deps) → Task 2 (independent) → Task 3 (consumes both). Task 3's `InboxPage` edits are the highest-risk; line numbers drift, so locate by the surrounding code shown, not the line number alone.
- Never run the full `pnpm test`; always target files as shown.
- Work on a worktree branch, not `main` (see Execution Handoff).
