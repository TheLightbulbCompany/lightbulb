# Teams Issue Linked-Approvals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** A "Linked approvals" section on the `/teams` issue-detail page (list + inline approve/reject), reusing E's `ApprovalRow`.

**Architecture:** Thin issue-scoped BFF passthrough (`GET /issues/{id}/approvals`); reuse `ApprovalRow`/`useApprovalMutations` from sub-project E with a status-gate + per-issue revalidation.

**Spec:** `docs/superpowers/specs/2026-05-29-teams-issue-approvals-design.md`

Conventions: backend from `apps/backend` (`uv run pytest <path> -q`); frontend from `apps/frontend` (`pnpm test -- <substr>`). Per-task targeted tests; full suite/tsc/build in the final task. SWR key for `read("/x")` is `/teams/x`.

---

## Task 1: BFF GET /issues/{id}/approvals

**Files:** Modify `apps/backend/core/services/paperclip_admin_client.py`, `apps/backend/routers/teams/issues.py`; Test `apps/backend/tests/unit/routers/teams/test_issue_approvals.py` (new).

- [ ] **Step 1: Failing test** — `tests/unit/routers/teams/test_issue_approvals.py`:

```python
# apps/backend/tests/unit/routers/teams/test_issue_approvals.py
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient
from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(user_id="u1", org_id="o1", owner_id="o1", company_id="co_abc",
                        paperclip_user_id="pcu", session_cookie="cookie")


@pytest.fixture
def client(teams_ctx):
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_list_issue_approvals_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_approvals = AsyncMock(return_value=[{"id": "ap1"}, {"id": "ap2"}])
    from routers.teams import agents as agents_mod
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/issues/iss1/approvals", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"approvals": [{"id": "ap1"}, {"id": "ap2"}]}
    admin.list_issue_approvals.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")
```

- [ ] **Step 2: Run → fail.** `uv run pytest tests/unit/routers/teams/test_issue_approvals.py -q`

- [ ] **Step 3: admin method** — after `get_issue` (or near the issue methods) in `paperclip_admin_client.py`:

```python
    async def list_issue_approvals(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the approvals linked to an issue.

        Maps to ``GET /api/issues/{issueId}/approvals`` (issue-scoped).
        """
        return await self._get(
            f"/api/issues/{issue_id}/approvals",
            session_cookie=session_cookie,
        )
```
(Verify `_get`'s exact param names against the existing `get_issue` and adapt if needed.)

- [ ] **Step 4: BFF route** — append to `routers/teams/issues.py` (near the children routes):

```python
@router.get("/issues/{issue_id}/approvals")
async def list_issue_approvals(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the approvals linked to an issue (the issue-detail Approvals section)."""
    rows = await _agents._admin().list_issue_approvals(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "approvals")
```
(`envelope_list` + `Depends` already imported.)

- [ ] **Step 5: Run → pass.** `uv run pytest tests/unit/routers/teams/test_issue_approvals.py -q` (1 pass). Ensure ruff clean (`uv run ruff check`/`format` the touched files).

- [ ] **Step 6: Commit** — `git commit -m "feat(teams): BFF GET /issues/{id}/approvals (linked approvals)"`

---

## Task 2: useApprovalMutations — revalidate per-issue approval lists

**Files:** Modify `apps/frontend/src/components/teams/inbox/hooks/useApprovalMutations.ts`; Test `apps/frontend/src/__tests__/teams/useApprovalMutations.issueKey.test.tsx` (new).

- [ ] **Step 1: Failing test:**

```tsx
// apps/frontend/src/__tests__/teams/useApprovalMutations.issueKey.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const post = vi.fn();
const swrMutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post }) }));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";

beforeEach(() => { post.mockReset().mockResolvedValue({}); swrMutate.mockReset(); });

describe("useApprovalMutations issue-approvals invalidation", () => {
  it("approve invalidates the per-issue approvals predicate (matches /teams/issues/X/approvals)", async () => {
    const { result } = renderHook(() => useApprovalMutations());
    await act(async () => { await result.current.approve("ap1"); });
    // One mutate call is a predicate fn; it must match issue-approval keys.
    const predicate = swrMutate.mock.calls.map((c) => c[0]).find((a) => typeof a === "function");
    expect(predicate).toBeTruthy();
    expect(predicate("/teams/issues/iss1/approvals")).toBe(true);
    expect(predicate("/teams/approvals")).toBe(false);
    expect(predicate("/teams/issues/iss1")).toBe(false);
  });
});
```

- [ ] **Step 2: Run → fail.** `pnpm test -- useApprovalMutations.issueKey`

- [ ] **Step 3: Implement** — in `useApprovalMutations.ts`, extend `revalidate`:

```ts
  const revalidate = useCallback(() => {
    mutate(APPROVALS_KEY);
    mutate(DASHBOARD_KEY);
    // Also refresh any per-issue linked-approvals lists (sub-project D2) so a
    // reused ApprovalRow on an issue-detail page updates after approve/reject.
    mutate((key) => typeof key === "string" && /^\/teams\/issues\/.+\/approvals$/.test(key));
  }, [mutate]);
```

- [ ] **Step 4: Run → pass.** `pnpm test -- useApprovalMutations` (existing + new pass).

- [ ] **Step 5: Commit** — `git commit -m "feat(teams): useApprovalMutations also revalidates per-issue approval lists"`

---

## Task 3: ApprovalRow — gate actions on pending status

**Files:** Modify `apps/frontend/src/components/teams/inbox/ApprovalRow.tsx`; Test extend `apps/frontend/src/__tests__/teams/ApprovalRow.test.tsx`.

- [ ] **Step 1: Failing test** (append to the existing `ApprovalRow.test.tsx`):

```tsx
  it("resolved approvals show a status chip and no action buttons", () => {
    render(<ApprovalRow approval={{ id: "a9", title: "Done one", status: "approved" }} />);
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^reject$/i })).toBeNull();
    expect(screen.getByText(/approved/i)).toBeInTheDocument();
  });
```
(The existing tests use `FRIDAY`-style fixtures? No — they pass an `Approval`. Keep the existing pending tests; they already assert the Approve/Reject buttons render for a pending approval. If the existing fixture omits `status`, ensure it is `status: "pending"` so the gate keeps the buttons.)

- [ ] **Step 2: Run → fail.** `pnpm test -- ApprovalRow`

- [ ] **Step 3: Implement** — in `ApprovalRow.tsx`:
  - Add `const isPending = approval.status === "pending";` near `title`.
  - Change the action block guard from `{!rejecting && (` to `{isPending && !rejecting && (`.
  - After that block (still inside the top `<div className="flex items-center gap-2">`), add a resolved chip:
    ```tsx
        {!isPending && (
          <span className="text-xs capitalize text-[var(--mute)]">{approval.status}</span>
        )}
    ```
  - The `rejecting` inline block stays as-is (only reachable when pending, since Reject is gated).

- [ ] **Step 4: Run → pass.** `pnpm test -- ApprovalRow` (existing pending tests + new resolved test). Also re-run `pnpm test -- NeedsAttentionBand` to confirm the E band still renders pending rows with actions.

- [ ] **Step 5: Commit** — `git commit -m "feat(teams): ApprovalRow shows actions only for pending (status chip otherwise)"`

---

## Task 4: IssueApprovalsSection + mount

**Files:** Create `apps/frontend/src/components/teams/issues/IssueApprovalsSection.tsx`; Modify `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx`; Test `apps/frontend/src/__tests__/teams/IssueApprovalsSection.test.tsx` (new); keep `IssueDetailPage.test.tsx` green.

- [ ] **Step 1: Failing test** — `IssueApprovalsSection.test.tsx`:

```tsx
// apps/frontend/src/__tests__/teams/IssueApprovalsSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));
vi.mock("@/components/teams/inbox/ApprovalRow", () => ({
  ApprovalRow: ({ approval }: { approval: { id: string; title?: string } }) => <li>row:{approval.id}</li>,
}));

import { IssueApprovalsSection } from "@/components/teams/issues/IssueApprovalsSection";

beforeEach(() => read.mockReset());

describe("IssueApprovalsSection", () => {
  it("renders nothing when there are no linked approvals", () => {
    read.mockReturnValue({ data: { approvals: [] }, isLoading: false, error: null, mutate: vi.fn() });
    const { container } = render(<IssueApprovalsSection issueId="iss1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders an Approvals header + a row per linked approval", () => {
    read.mockReturnValue({
      data: { approvals: [{ id: "ap1", status: "pending" }, { id: "ap2", status: "approved" }] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueApprovalsSection issueId="iss1" />);
    expect(screen.getByText(/Approvals/)).toBeInTheDocument();
    expect(screen.getByText("row:ap1")).toBeInTheDocument();
    expect(screen.getByText("row:ap2")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run → fail.** `pnpm test -- IssueApprovalsSection`

- [ ] **Step 3: Implement** — `IssueApprovalsSection.tsx`:

```tsx
// apps/frontend/src/components/teams/issues/IssueApprovalsSection.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

export function IssueApprovalsSection({ issueId }: { issueId: string }) {
  const { read } = useTeamsApi();
  const { data } = read<{ approvals: Approval[] }>(`/issues/${issueId}/approvals`);
  const approvals = data?.approvals ?? [];
  if (approvals.length === 0) return null;
  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label"><b>Approvals</b></h2>
      <ul className="flex flex-col">
        {approvals.map((a) => (
          <ApprovalRow key={a.id} approval={a} />
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 4: Mount in `IssueDetailPage.tsx`** — add import `import { IssueApprovalsSection } from "./IssueApprovalsSection";`, and render it after `<SubIssuesSection .../>` and before the Conversation block:
```tsx
        <SubIssuesSection ... />
        <IssueApprovalsSection issueId={issueId} />
        <div className="pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
          <h2 className="section-label mb-3"><b>Conversation</b></h2>
```

- [ ] **Step 5: Keep `IssueDetailPage.test.tsx` green** — the page now also calls `read("/issues/{id}/approvals")`. Its `read` mock already returns a benign default `{ data: { issues: [] }, ... }` for unknown paths (from D1); `IssueApprovalsSection` reads `.approvals` → `undefined ?? []` → renders nothing. Confirm the default branch covers `/approvals` (no `.approvals` key → empty). If the mock's default lacks an `approvals` key it still works (`data.approvals` is undefined → `[]`). Run `pnpm test -- IssueDetailPage` → pass; adjust only if needed (do not weaken assertions).

- [ ] **Step 6: Run → pass.** `pnpm test -- IssueApprovalsSection IssueDetailPage`

- [ ] **Step 7: Commit** — `git commit -m "feat(teams): IssueApprovalsSection mounted on issue detail"`

---

## Task 5: Full verification + roadmap

- [ ] **Step 1:** Backend — `uv run pytest tests/unit/routers/teams/ -q` (all pass).
- [ ] **Step 2:** `pnpm exec tsc --noEmit` (clean).
- [ ] **Step 3:** `pnpm exec eslint src/components/teams/issues src/components/teams/inbox/ApprovalRow.tsx src/components/teams/inbox/hooks/useApprovalMutations.ts` (0 errors).
- [ ] **Step 4:** `pnpm exec vitest run` — all D2 + E approval tests pass; only the pre-existing local-env-only files (CreditsPanel/ChatGPTOAuthStep/useChatGPTOAuth/BotSetupWizard) may fail locally. (vitest may hang at teardown — read per-file results.)
- [ ] **Step 5:** `pnpm run build` — "✓ Compiled successfully" (local Clerk prerender of /admin or /_not-found is env-only).
- [ ] **Step 6:** Roadmap: note D2 alongside D1 under row D. Commit.

---

## Self-Review
- Spec coverage: list endpoint (T1), per-issue revalidation (T2), status gate (T3), section + mount (T4). Reuse ApprovalRow + useApprovalMutations. Link/unlink + detail deferred (correct).
- Type consistency: `list_issue_approvals(issue_id, session_cookie)` (T1) used identically; `IssueApprovalsSection({issueId})` matches mount (T4); SWR key `/teams/issues/{id}/approvals` consistent across T2 predicate + T4 read.
- Placeholder scan: none.
