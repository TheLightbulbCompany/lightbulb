# Teams Inbox Atoms Port (PR 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the existing /teams Inbox surface (`apps/frontend/src/components/teams/inbox/*`, 2190 LOC) to compose the editorial atom layer that PR 1 (#553) shipped. Restores cross-tab consistency by replacing inline `editorial-btn` strings + ad-hoc shadcn `Button` imports + hand-rolled tabs with the new `EditorialButton`, `PanelToolbar`, `PanelMasthead`, `PanelTabs`, `PanelEmptyState` atoms.

**Architecture:** Surgical refactor, not a rewrite — the inbox already has substantial functionality from PR #533/#535/#536. Each task touches one file at a time, replaces one ad-hoc pattern with the corresponding atom, runs the existing test suite to confirm no behavior regression, and commits. The `Monogram`, `StatusDot`, `AgentPill`, `SectionLabel` identity atoms from `apps/frontend/src/components/agents/AgentIdentity.tsx` stay — they remain the right primitives for issue-row contents (assignee chip, status dot). Adds one missing BFF route (`GET /issues/labels`) for the inbox filter chip dropdown.

**Tech Stack:** Next.js 16 App Router, React 19, Tailwind CSS v4, shadcn/ui, vitest + @testing-library/react. Atoms from `apps/frontend/src/components/teams/shell/*` (PR 1, merged at `68d559cc`).

**Spec:** `docs/superpowers/specs/2026-05-07-teams-panel-port-editorial-atoms-design.md` (Inbox section).

**Branch:** `plan/teams-inbox-atoms-port` (already cut off `origin/main` post-PR-1-merge).

---

## File Structure

**Frontend — Modify:**
- `apps/frontend/src/components/teams/inbox/InboxToolbar.tsx` — replace inline `editorial-btn` strings + search input with `<PanelToolbar>` + `<EditorialButton>` from `@/components/teams/shell`.
- `apps/frontend/src/components/teams/inbox/InboxPage.tsx` — apply `<PanelMasthead>` for the header and `<PanelTabs>` for the tab strip.
- `apps/frontend/src/components/teams/inbox/InboxList.tsx` — replace `Button` from `@/components/ui/button` with `EditorialButton`. Replace inline empty-state markup with `<PanelEmptyState>`.
- `apps/frontend/src/components/teams/inbox/IssueRow.tsx` — replace any inline `editorial-btn` action button with `<EditorialButton variant="ghost" size="icon">`.
- `apps/frontend/src/app/api/...` — none. Frontend changes only on the panel side.

**Backend — Add (one route):**
- `apps/backend/routers/teams/issues.py` — add `GET /issues/labels` route delegating to a new admin-client method.
- `apps/backend/core/services/paperclip_admin_client.py` — add `list_company_labels(session_cookie, company_id)` method calling Paperclip's `/api/companies/{co}/labels`.

**Tests — Modify or Add:**
- `apps/backend/tests/unit/routers/test_teams_issues.py` (or wherever existing /teams/issues tests live) — add coverage for the new `GET /issues/labels` route.
- `apps/frontend/tests/unit/components/teams/inbox/InboxToolbar.test.tsx` — assert PanelToolbar usage + EditorialButton.
- `apps/frontend/tests/unit/components/teams/inbox/InboxPage.test.tsx` (already exists per existing test count) — update to expect PanelMasthead role="banner" + PanelTabs role="tablist".
- `apps/frontend/tests/unit/components/teams/inbox/InboxList.test.tsx` — assert PanelEmptyState renders for zero rows + EditorialButton replaces shadcn Button.
- `apps/frontend/tests/unit/components/teams/inbox/IssueRow.test.tsx` — assert action buttons render via EditorialButton.

---

## Task 0: Setup — verify branch + audit existing inbox surface

**Files:** none (read-only).

- [ ] **Step 1: Confirm branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git status
git log --oneline -1
```

Expected: on branch `plan/teams-inbox-atoms-port`, top commit is the squash-merged PR 1 (commit message starts with `feat(teams-shell): editorial atom library`).

- [ ] **Step 2: Confirm shell atoms are importable**

```sh
cd apps/frontend && pnpm tsc --noEmit
```

Expected: no errors. (Sanity check that the merged atoms compile against the current tree.)

- [ ] **Step 3: Run baseline tests**

```sh
cd apps/frontend && pnpm test
```

Expected: same baseline as PR 1 — 974+ pass, 10 pre-existing failures unchanged. Confirms we're starting from a clean slate.

- [ ] **Step 4: List existing inbox files (no commit)**

```sh
ls apps/frontend/src/components/teams/inbox/
wc -l apps/frontend/src/components/teams/inbox/*.tsx
```

Expected: `InboxList.tsx`, `InboxPage.tsx`, `InboxToolbar.tsx`, `IssueColumns.tsx`, `IssueFiltersPopover.tsx`, `IssueRow.tsx`, `KeyboardShortcutsCheatsheet.tsx`, `SwipeToArchive.tsx`. ~2190 LOC total.

No commit (read-only verification).

---

## Task 1: Add `GET /issues/labels` BFF route

The Inbox filter-chip dropdown lists company-wide labels. Upstream Paperclip exposes `GET /api/companies/{co}/labels`; our BFF doesn't proxy it yet.

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (add `list_company_labels` method)
- Modify: `apps/backend/routers/teams/issues.py` (add `@router.get("/issues/labels")` handler)
- Test: `apps/backend/tests/unit/routers/test_teams_issues.py` (or existing teams issues test file — locate first)

- [ ] **Step 1: Locate existing teams/issues tests**

```sh
find apps/backend/tests -name "test_teams_issues*" -o -name "*teams*issues*test*"
```

Expected: a test file path for /teams/issues route handlers. Note the path for the test step below. If no file exists, create at `apps/backend/tests/unit/routers/teams/test_issues.py`.

- [ ] **Step 2: Write the failing test**

In the test file located in step 1, add this test (adjust import path + fixture style to match the file's existing patterns):

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_list_labels_returns_envelope():
    """GET /teams/issues/labels proxies upstream + wraps in {labels: [...]} envelope."""
    from fastapi.testclient import TestClient
    from main import app
    from core.auth import AuthContext, get_current_user
    from routers.teams import agents as _agents
    from routers.teams.deps import TeamsContext

    async def _ctx_override():
        return TeamsContext(
            user_id="user_test",
            org_id=None,
            owner_id="user_test",
            company_id="comp_test",
            paperclip_user_id="pcp_test",
            session_cookie="cookie_test",
        )

    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_test", email="t@example.com"
    )
    app.dependency_overrides[_agents._ctx] = _ctx_override

    mock_admin = MagicMock()
    mock_admin.list_company_labels = AsyncMock(
        return_value=[
            {"id": "lbl_1", "name": "bug", "color": "#dc2626"},
            {"id": "lbl_2", "name": "feature", "color": "#16a34a"},
        ]
    )
    with patch.object(_agents, "_admin", lambda: mock_admin):
        client = TestClient(app)
        resp = client.get("/api/v1/teams/issues/labels")

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert "labels" in body
    assert len(body["labels"]) == 2
    assert body["labels"][0]["name"] == "bug"
    mock_admin.list_company_labels.assert_called_once_with(
        company_id="comp_test", session_cookie="cookie_test"
    )
```

- [ ] **Step 3: Run test to verify failure**

```sh
cd apps/backend && uv run pytest <path-from-step-1> -k test_list_labels_returns_envelope -v
```

Expected: FAIL with either "404 Not Found" (route doesn't exist) or "AttributeError: ... has no attribute 'list_company_labels'".

- [ ] **Step 4: Add `list_company_labels` to `PaperclipAdminClient`**

In `apps/backend/core/services/paperclip_admin_client.py`, add this method alongside the other issues methods (locate the `list_issues` method and add after the `add_issue_comment` method block; preserve the file's existing style):

```python
    async def list_company_labels(
        self,
        *,
        company_id: str,
        session_cookie: str,
    ) -> list[dict]:
        """List labels defined on a Paperclip company.

        Maps to ``GET /api/companies/{companyId}/labels`` upstream. Returns
        a list of ``{id, name, color}`` dicts. Used by the Teams Inbox filter
        chip dropdown to populate selectable labels for the issues filter.

        Returns an empty list if upstream returns a non-list payload (defensive
        — upstream's labels endpoint is documented to return a JSON array per
        ``paperclip/server/src/routes/labels.ts``).
        """
        result = await self._get(
            f"/api/companies/{company_id}/labels",
            session_cookie=session_cookie,
        )
        # Upstream returns a bare list `res.json(labels)`; unwrap defensively
        # in case a future upstream change envelopes it.
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            inner = result.get("labels") or result.get("items") or []
            return inner if isinstance(inner, list) else []
        return []
```

- [ ] **Step 5: Add the BFF route**

In `apps/backend/routers/teams/issues.py`, add this route (place it alongside the other GET handlers, after `list_issues` and before any POST handlers; match the file's existing pattern of using `_ctx` and `_admin()` from the agents module):

```python
from .deps import envelope_list  # already imported in this file — verify before adding

@router.get("/issues/labels")
async def list_labels(ctx: TeamsContext = Depends(_ctx)):
    """List company labels for the Inbox filter chip dropdown.

    Wraps upstream's bare-array response in ``{labels: [...]}`` envelope so
    the frontend's panel hooks can read ``data?.labels`` like every other
    Teams BFF resource. See ``deps.envelope_list`` for the wrapping pattern.
    """
    upstream = await _agents._admin().list_company_labels(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "labels")
```

If `_agents` isn't imported in `issues.py` yet, add `from . import agents as _agents` to the top of the file.

- [ ] **Step 6: Run test, verify pass**

```sh
cd apps/backend && uv run pytest <path-from-step-1> -k test_list_labels_returns_envelope -v
```

Expected: PASS.

- [ ] **Step 7: Run the full backend teams test suite to confirm no regression**

```sh
cd apps/backend && uv run pytest tests/unit/routers/teams/ -v
```

Expected: all teams BFF tests pass.

- [ ] **Step 8: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/backend/core/services/paperclip_admin_client.py \
  apps/backend/routers/teams/issues.py \
  <test-file-path-from-step-1>
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-bff): GET /issues/labels for Inbox filter chip dropdown"
```

---

## Task 2: Refactor `InboxToolbar` to use `PanelToolbar` + `EditorialButton`

Currently the file uses inline `editorial-btn` class strings (e.g. `className="editorial-btn editorial-btn--secondary editorial-btn--sm shrink-0"`) on raw `<button>` elements + a hand-rolled search input. Replace with the atom layer.

**Files:**
- Modify: `apps/frontend/src/components/teams/inbox/InboxToolbar.tsx` (269 LOC)
- Test: `apps/frontend/tests/unit/components/teams/inbox/InboxToolbar.test.tsx` (verify file exists; if not, create)

- [ ] **Step 1: Read the current file end-to-end**

```sh
cat apps/frontend/src/components/teams/inbox/InboxToolbar.tsx
```

Catalog: how many `<button className="editorial-btn ...">` instances exist; what props each takes; whether there's a search input + filter chips slot pattern.

- [ ] **Step 2: Locate or create the test file**

```sh
ls apps/frontend/tests/unit/components/teams/inbox/InboxToolbar.test.tsx 2>&1
```

If the file doesn't exist, create it. If it exists, read it to understand current coverage.

- [ ] **Step 3: Write the (new or updated) failing test**

If creating new, write a smoke test:

```tsx
// apps/frontend/tests/unit/components/teams/inbox/InboxToolbar.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxToolbar } from "@/components/teams/inbox/InboxToolbar";

// Match the actual props InboxToolbar takes — read the current component file
// in step 1 and adapt this fixture if the prop shape differs.
const baseProps = {
  searchValue: "",
  onSearchChange: vi.fn(),
  onCreateIssue: vi.fn(),
  // ... add other required props from the actual InboxToolbar signature
};

describe("InboxToolbar", () => {
  it("renders the editorial search input via PanelToolbar (role=searchbox)", () => {
    render(<InboxToolbar {...baseProps} />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("primary action uses EditorialButton (renders a real <button>)", () => {
    render(<InboxToolbar {...baseProps} />);
    // The "+ New issue" or equivalent CTA should be a button (EditorialButton renders <button>)
    const buttons = screen.getAllByRole("button");
    expect(buttons.length).toBeGreaterThan(0);
  });
});
```

If a test file already exists, ADD a test that asserts the toolbar renders `role="searchbox"` (which is what `PanelToolbar` produces).

- [ ] **Step 4: Run test, verify failure**

```sh
cd apps/frontend && pnpm test InboxToolbar -- --run
```

Expected: FAIL — the searchbox role assertion fails because the current input is `<input type="text">` not `<input type="search">`. Or, if the test was new, may fail on missing props — adapt to match the actual component signature.

- [ ] **Step 5: Refactor `InboxToolbar.tsx` to compose `PanelToolbar` + `EditorialButton`**

Read the existing file. Identify the search input region — replace with `<PanelToolbar searchValue={...} onSearchChange={...} searchPlaceholder={...}>` consuming the same props the original input took. Place existing filter chips in `PanelToolbar`'s `filters` slot. Place the primary action (e.g. "+ New issue" button) in the `actions` slot wrapped in `<EditorialButton variant="primary">`.

For any other inline `editorial-btn` buttons in the file (secondary actions like sort/group toggles), replace with `<EditorialButton variant="ghost" size="sm">` or appropriate variant.

Add to imports at top of file:

```tsx
import { PanelToolbar, EditorialButton } from "@/components/teams/shell";
```

Remove the inline `editorial-btn` className strings and the hand-rolled search input markup. The component file should shrink (estimated 269 LOC → ~180-200 LOC).

- [ ] **Step 6: Run test, verify pass**

```sh
cd apps/frontend && pnpm test InboxToolbar -- --run
```

Expected: PASS, including the new `searchbox` role assertion.

- [ ] **Step 7: Run full inbox test suite to confirm no behavioral regression**

```sh
cd apps/frontend && pnpm test "components/teams/inbox" -- --run
```

Expected: all existing inbox tests still pass.

- [ ] **Step 8: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/inbox/InboxToolbar.tsx \
  apps/frontend/tests/unit/components/teams/inbox/InboxToolbar.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-inbox): InboxToolbar composes PanelToolbar + EditorialButton"
```

---

## Task 3: Refactor `InboxPage` header to use `PanelMasthead`

The inbox page renders a top header strip; replace with the editorial masthead atom.

**Files:**
- Modify: `apps/frontend/src/components/teams/inbox/InboxPage.tsx` (292 LOC)
- Test: `apps/frontend/tests/unit/components/teams/inbox/InboxPage.test.tsx` (verify exists)

- [ ] **Step 1: Read the current InboxPage header region**

```sh
sed -n '1,80p' apps/frontend/src/components/teams/inbox/InboxPage.tsx
```

Locate the `<header>` or top heading element. Note its overline text, title text, and any meta/date/actions on the right.

- [ ] **Step 2: Locate the test file**

```sh
ls apps/frontend/tests/unit/components/teams/inbox/InboxPage.test.tsx 2>&1
```

If exists, read to understand current assertions.

- [ ] **Step 3: Add a failing test asserting `<PanelMasthead>` rendering**

Add to `InboxPage.test.tsx`:

```tsx
it("renders the editorial PanelMasthead with overline 'Inbox' and h1 title", () => {
  render(<InboxPage companyId="c1" currentUserId="u1" />);
  // PanelMasthead renders <header role="banner"> with an h1 title
  expect(screen.getByRole("banner")).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 1, name: /inbox/i })).toBeInTheDocument();
});
```

(Adjust prop fixture if `InboxPage` requires more props — read the existing test for shape.)

- [ ] **Step 4: Run test, verify failure**

```sh
cd apps/frontend && pnpm test InboxPage -- --run
```

Expected: FAIL — no `role="banner"` element until the masthead is added.

- [ ] **Step 5: Replace the existing header with `<PanelMasthead overline="Inbox" title="Inbox" />`**

Add to imports:

```tsx
import { PanelMasthead } from "@/components/teams/shell";
```

Replace the existing header block with:

```tsx
<PanelMasthead
  overline="Inbox"
  title="Inbox"
  // meta + actions slots only if the existing header had them — otherwise omit
/>
```

Keep all other rendering logic untouched.

- [ ] **Step 6: Run test, verify pass**

```sh
cd apps/frontend && pnpm test InboxPage -- --run
```

Expected: PASS.

- [ ] **Step 7: Run full inbox test suite**

```sh
cd apps/frontend && pnpm test "components/teams/inbox" -- --run
```

Expected: all pass.

- [ ] **Step 8: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/inbox/InboxPage.tsx \
  apps/frontend/tests/unit/components/teams/inbox/InboxPage.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-inbox): InboxPage header uses PanelMasthead"
```

---

## Task 4: Refactor `InboxPage` tab strip to use `PanelTabs`

The inbox has tabs (default / mine / unread / etc per the BFF's `_TAB_FILTERS` map). The current implementation likely has a hand-rolled tab strip.

**Files:**
- Modify: `apps/frontend/src/components/teams/inbox/InboxPage.tsx`
- Test: `apps/frontend/tests/unit/components/teams/inbox/InboxPage.test.tsx`

- [ ] **Step 1: Find the existing tab implementation**

```sh
grep -n "tab\|Tab" apps/frontend/src/components/teams/inbox/InboxPage.tsx | head -20
```

Catalog the tabs (their `value`s and `label`s).

- [ ] **Step 2: Add a failing test for `role="tablist"` and the tab labels**

```tsx
it("renders inbox tabs via PanelTabs (role=tablist)", () => {
  render(<InboxPage companyId="c1" currentUserId="u1" />);
  expect(screen.getByRole("tablist")).toBeInTheDocument();
  // Confirm the actual tab labels — adjust the labels to whatever this surface uses today
  expect(screen.getByRole("tab", { name: /mine/i })).toBeInTheDocument();
});
```

(Replace `/mine/i` with whatever the FIRST tab's label is per the existing component. Read the component first.)

- [ ] **Step 3: Run test, verify failure**

```sh
cd apps/frontend && pnpm test InboxPage -- --run
```

Expected: FAIL — `role="tablist"` not found.

- [ ] **Step 4: Replace hand-rolled tab strip with `<PanelTabs>`**

Add import:

```tsx
import { PanelTabs, type PanelTabDefinition } from "@/components/teams/shell";
```

Replace the existing tab strip with:

```tsx
const INBOX_TABS: PanelTabDefinition[] = [
  { value: "mine", label: "Mine" },
  // ... mirror whatever tabs already exist in the file
];

<PanelTabs
  tabs={INBOX_TABS}
  value={activeTab}
  onValueChange={setActiveTab}
/>
```

Wire `activeTab` and `setActiveTab` to whatever state the existing component already manages.

- [ ] **Step 5: Run test, verify pass**

```sh
cd apps/frontend && pnpm test InboxPage -- --run
```

Expected: PASS.

- [ ] **Step 6: Run full inbox test suite**

```sh
cd apps/frontend && pnpm test "components/teams/inbox" -- --run
```

Expected: all pass.

- [ ] **Step 7: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/inbox/InboxPage.tsx \
  apps/frontend/tests/unit/components/teams/inbox/InboxPage.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-inbox): InboxPage tab strip uses PanelTabs"
```

---

## Task 5: Refactor `InboxList` empty states to use `PanelEmptyState`

The `InboxList` component renders the issue list. When empty, it currently shows ad-hoc text (or a `<div>No items.</div>`-style stub). Replace with the editorial empty-state atom.

**Files:**
- Modify: `apps/frontend/src/components/teams/inbox/InboxList.tsx` (132 LOC)
- Test: `apps/frontend/tests/unit/components/teams/inbox/InboxList.test.tsx`

- [ ] **Step 1: Find the empty-state branch in the existing file**

```sh
grep -n "empty\|Empty\|zero\|Inbox zero\|No \|no items" apps/frontend/src/components/teams/inbox/InboxList.tsx
```

Note the existing copy (e.g. "Inbox zero." / "No new inbox items.").

- [ ] **Step 2: Add a failing test asserting `PanelEmptyState` shape**

In the test file (locate or create), add:

```tsx
it("renders PanelEmptyState shape when issues list is empty", () => {
  render(<InboxList issues={[]} /* + other required props */ />);
  // PanelEmptyState renders a Lora-italic <h2> headline
  const headline = screen.getByRole("heading", { level: 2 });
  expect(headline).toHaveClass("font-editorial-serif");
});
```

(Match the actual InboxList prop signature.)

- [ ] **Step 3: Run test, verify failure**

```sh
cd apps/frontend && pnpm test InboxList -- --run
```

Expected: FAIL — no `<h2 class="font-editorial-serif">` exists yet (current empty branch is a plain text node).

- [ ] **Step 4: Replace empty branch with `<PanelEmptyState>`**

Add import:

```tsx
import { PanelEmptyState } from "@/components/teams/shell";
import { Inbox } from "lucide-react";
```

In the empty-state render branch, replace the existing markup with:

```tsx
<PanelEmptyState
  icon={Inbox}
  headline="Inbox zero."
  supporting="Nothing pending right now. Items appear here when an agent completes a run, an issue is assigned to you, or an approval is requested."
/>
```

(Tune copy to match the existing or upstream's voice.)

- [ ] **Step 5: Replace `Button` from shadcn with `EditorialButton` (file-wide)**

If the file imports `Button` from `@/components/ui/button`, replace each usage with `EditorialButton` from `@/components/teams/shell`. Map shadcn `variant` props to EditorialButton variants:

- shadcn `variant="default"` → EditorialButton `variant="primary"`
- shadcn `variant="secondary"` → EditorialButton `variant="secondary"`
- shadcn `variant="ghost"` → EditorialButton `variant="ghost"`
- shadcn `variant="destructive"` → EditorialButton `variant="destructive"`
- shadcn `variant="outline"` → EditorialButton `variant="secondary"` (closest editorial equivalent)

Drop the shadcn `Button` import; add `EditorialButton` to the existing `@/components/teams/shell` import line.

- [ ] **Step 6: Run test, verify pass**

```sh
cd apps/frontend && pnpm test InboxList -- --run
```

Expected: PASS.

- [ ] **Step 7: Run full inbox test suite**

```sh
cd apps/frontend && pnpm test "components/teams/inbox" -- --run
```

Expected: all pass.

- [ ] **Step 8: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/inbox/InboxList.tsx \
  apps/frontend/tests/unit/components/teams/inbox/InboxList.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-inbox): InboxList empty state uses PanelEmptyState; Button → EditorialButton"
```

---

## Task 6: Refactor `IssueRow` action buttons to use `EditorialButton`

`IssueRow` is the per-row component. It likely has inline action buttons (archive, mark-read, etc.) using ad-hoc class strings. Standardize on `<EditorialButton variant="ghost" size="icon">`.

**Files:**
- Modify: `apps/frontend/src/components/teams/inbox/IssueRow.tsx` (266 LOC)
- Test: `apps/frontend/tests/unit/components/teams/inbox/IssueRow.test.tsx`

- [ ] **Step 1: Find existing action buttons**

```sh
grep -nE 'editorial-btn|<button|onClick' apps/frontend/src/components/teams/inbox/IssueRow.tsx | head -15
```

Catalog: how many action buttons exist; their lucide-react icons; their click handlers.

- [ ] **Step 2: Add a failing test (verify icon-button accessibility)**

In the test file, add:

```tsx
it("action buttons are EditorialButton-shaped (real <button> with accessible name)", () => {
  const issue = /* a minimal valid issue fixture */;
  render(<IssueRow issue={issue} onArchive={vi.fn()} /* + other required props */ />);
  // Each action button should have an accessible name (aria-label or visible text)
  const archiveBtn = screen.getByRole("button", { name: /archive/i });
  expect(archiveBtn).toBeInTheDocument();
});
```

- [ ] **Step 3: Run test, verify failure**

```sh
cd apps/frontend && pnpm test IssueRow -- --run
```

Expected: FAIL if archive button currently lacks an accessible name (icon-only with no `aria-label`).

- [ ] **Step 4: Replace inline buttons with `<EditorialButton variant="ghost" size="icon" aria-label={...}>`**

For each action button in the file:

```tsx
<EditorialButton
  variant="ghost"
  size="icon"
  aria-label="Archive issue"
  onClick={() => onArchive(issue.id)}
>
  <Archive className="h-4 w-4" />
</EditorialButton>
```

Add import:

```tsx
import { EditorialButton } from "@/components/teams/shell";
```

Drop any `editorial-btn ...` inline class strings from the action buttons.

- [ ] **Step 5: Run test, verify pass**

```sh
cd apps/frontend && pnpm test IssueRow -- --run
```

Expected: PASS.

- [ ] **Step 6: Run full inbox test suite**

```sh
cd apps/frontend && pnpm test "components/teams/inbox" -- --run
```

Expected: all pass.

- [ ] **Step 7: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/inbox/IssueRow.tsx \
  apps/frontend/tests/unit/components/teams/inbox/IssueRow.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-inbox): IssueRow action buttons use EditorialButton w/ aria-label"
```

---

## Task 7: Final verify + push + open PR

**Files:** none (verification + push only).

- [ ] **Step 1: Run the full frontend test suite**

```sh
cd apps/frontend && pnpm test
```

Expected: all atom + inbox tests pass; baseline 10 pre-existing failures unchanged. If new failures appear in non-inbox files, investigate before pushing.

- [ ] **Step 2: Run lint**

```sh
cd apps/frontend && pnpm lint
```

Expected: no errors.

- [ ] **Step 3: Run type check**

```sh
cd apps/frontend && pnpm tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Run backend test suite (because Task 1 added backend code)**

```sh
cd apps/backend && uv run pytest tests/unit/routers/teams/ tests/test_paperclip_admin_client.py -v
```

Expected: all pass.

- [ ] **Step 5: Push branch**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix push -u origin plan/teams-inbox-atoms-port
```

- [ ] **Step 6: Open the PR**

```sh
gh pr create --title "feat(teams-inbox): port to editorial atom layer (PR 2 of /teams port)" --body "$(cat <<'EOF'
## Summary

Second PR of the multi-step /teams panel port (see spec at `docs/superpowers/specs/2026-05-07-teams-panel-port-editorial-atoms-design.md` and plan at `docs/superpowers/plans/2026-05-07-teams-inbox-atoms-port.md`). Refactors the existing Inbox surface (`apps/frontend/src/components/teams/inbox/*`, 2190 LOC) to compose the editorial atom layer that PR 1 (#553) shipped.

This is a refactor, not a rewrite — the inbox already had substantial functionality from PR #533/#535/#536. PR 2 makes that functionality consistent with the editorial design language and the panel-shell atoms.

## What changed

- **Backend:** added `GET /teams/issues/labels` to power the Inbox filter chip dropdown. Wraps upstream Paperclip's bare-array labels response in our `{labels: [...]}` envelope.
- **Frontend — InboxToolbar:** composes `<PanelToolbar>` (search input + filter slot + actions slot) and `<EditorialButton>` for the primary action. Drops inline `editorial-btn` class strings.
- **Frontend — InboxPage:** header → `<PanelMasthead overline="Inbox" title="Inbox" />`. Tab strip → `<PanelTabs tabs={INBOX_TABS} value={activeTab} onValueChange={setActiveTab} />`.
- **Frontend — InboxList:** empty-state branch → `<PanelEmptyState icon={Inbox} ...>`. Replaces shadcn `Button` imports with `EditorialButton`.
- **Frontend — IssueRow:** action buttons (archive, mark-read, etc.) → `<EditorialButton variant="ghost" size="icon" aria-label="...">`. Adds proper accessible names.

## Test plan

- [x] `pytest apps/backend/tests/unit/routers/teams/` — passes including new `/issues/labels` test
- [x] `pnpm test` (frontend full suite) — inbox tests pass, no regressions
- [x] `pnpm tsc --noEmit` — clean
- [x] `pnpm lint` — clean
- [ ] Manual visual smoke on dev: load `/teams` → Inbox tab. Verify masthead, tab strip, search, empty state, and per-row action buttons all render in editorial cream/forest tokens with no visual break versus current dev.

## Next

PR 3: Routines port — needs more BFF route additions (`POST /routines`, `POST /routines/{id}/run-now`, `POST /routines/{id}/pause` + `/resume`, `GET /routines/{id}/runs`). See the spec for full panel sequencing.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## Self-review notes

**Spec coverage (Inbox section of `docs/superpowers/specs/2026-05-07-teams-panel-port-editorial-atoms-design.md`):**
- Tabs (default/unread/recent/archived/etc.) — ✅ existing tabs kept; refactored to use `<PanelTabs>` in Task 4. (Spec listed more tabs than the BFF's `_TAB_FILTERS` map currently exposes — additional tabs would require BFF + UI work beyond this PR's scope and are deferred.)
- Toolbar (search + filter pills + sort + group + view toggle + keyboard cheatsheet) — partially covered: search via `<PanelToolbar>` in Task 2, filter chips kept (passed through `filters` slot). Sort/group/view-toggle and keyboard-shortcuts cheatsheet are existing PR #533 features that this PR doesn't restructure further.
- Issue rows (priority dot + status dot + title + project pill + assignee monogram + relative time + action menu) — existing `IssueRow.tsx` already renders these via the old `Monogram`/`AgentPill`/`StatusDot` atoms; this PR only refactors the action buttons (Task 6).
- Live-run badges — already shipped via `useLiveRuns` per existing inbox surface; not restructured.
- Empty states (per-tab Inbox zero etc.) — ✅ Task 5.
- Backend `POST /inbox/{id}/snooze` and `POST /inbox/mark-read` (bulk) — **deviated**: spec was wrong; upstream uses per-issue `markRead`/`markUnread` (already proxied) and has no inbox-snooze. Bulk mark-read is client-side parallel calls. PR 2's BE work is just `GET /issues/labels` (Task 1) — the actual gap.

**Placeholder scan:** No "TBD" / "TODO" / "fill in later". Each step has runnable code and exact paths. The two places marked "(adjust to match the actual prop signature)" are explicit instructions for the implementer to read the existing file first — not placeholders for the plan itself.

**Type consistency:**
- `list_company_labels(company_id, session_cookie)` (kw-only) — declared in Task 1 step 4, called from Task 1 step 5 with the same kw signature ✓
- `envelope_list(upstream, "labels")` (positional) — matches existing usage in `routers/teams/agents.py:228` ✓
- `PanelTabDefinition` — matches the type exported from `@/components/teams/shell` (PR 1 barrel) ✓
- `PanelToolbar` props (`searchValue`, `onSearchChange`, `searchPlaceholder`, `filters`, `actions`) — match the atom's signature ✓
- `EditorialButton` variants (`primary`/`secondary`/`ghost`/`destructive`) and sizes (`sm`/`md`/`lg`/`icon`) — match Task 1 of PR 1's plan after the size-naming unification ✓

No gaps detected.

**Out-of-scope (deferred to future PRs):**
- Sort / group / view-toggle restructuring in InboxToolbar (existing PR #533 features kept as-is).
- KeyboardShortcutsCheatsheet refactor — not strictly needed for the editorial-atom adoption.
- Per-tab keyboard shortcuts beyond what's already shipped.
- Any IssueColumns refactor — file is 464 LOC and largely concerned with status/priority cell rendering using the old Monogram/StatusDot atoms, which stay.
