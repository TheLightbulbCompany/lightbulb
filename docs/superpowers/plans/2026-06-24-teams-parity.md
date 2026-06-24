# Teams UI Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. One long-lived branch `feat/teams-parity`; ONE PR at the end; merge once; then verify live in claude-in-chrome on prod.

**Goal:** Bring our rebuilt Teams UI (cream/forest editorial theme) to feature + polish parity with upstream Paperclip across task-detail, the New-task composer, the Tasks list/board, single-field issue properties, routines/inbox, and the executionPolicy/Monitor cluster.

**Architecture:** The Teams BFF (`apps/backend/routers/teams/`) proxies a per-tenant Paperclip; create/patch bodies are `extra="forbid"` Pydantic whitelists that forward verbatim (camelCase via `alias_generator=to_camel`, `by_alias=True`) to Paperclip's own validators. Most backend gaps = widen one whitelist field; a few need a new proxy route + `PaperclipAdminClient` method. The frontend is Next.js 16 / React 19 / SWR / lucide; mutations flow through `useIssueMutations` → `useTeamsApi` (`PATCH /teams/issues/{id}`). Several upstream-ported components already exist unused and just need wiring.

**Tech Stack:** FastAPI + httpx (BFF), Next.js App Router, SWR, Tailwind v4 CSS-variable theme, Pydantic v2, pytest, vitest.

## Global Constraints

- **Theme discipline:** cream/forest editorial theme on CSS variables only (`--cream --cream-deeper --ink --ink-soft --mute --edge --forest --danger --warn`). NO raw hex/`rgba()`, NO shadcn semantic classes (`text-muted-foreground`, `bg-accent`, `bg-primary`, `border-border`), NO raw shadcn `Dialog`/`DialogContent` without theme overrides (its default is a black modal on cream). Use the existing `EditorialDialog`, `EditorialButton`, `MenuPopover`, `PropertyRow`, `StatusIcon`, `PriorityIcon`, `AssigneePicker`, `ProjectPicker`, `Identity` primitives.
- **BFF schema discipline:** every new request-body field goes on a `_Strict` subclass (`extra="forbid"`); snake_case Python attr + `to_camel` alias; routers forward with `model_dump(..., by_alias=True)`. Create uses `exclude_none=True`; patch uses `exclude_unset=True`.
- **Terminology:** user-facing copy says **"task"**, not "issue" (board is "Tasks"). Internal type/var names may stay `Issue`.
- **Comments:** explain WHY, never reference PR numbers / bug IDs / audit rounds.
- **No backwards-compat noise:** strict patterns matching current usage; don't widen regex "for old values".
- **Tests:** backend = pytest (annotate nothing special). Frontend CI runs `tsc --noEmit` over TEST files too — annotate mock-builder return types so type-incomplete mocks don't pass vitest locally then fail CI. NEVER run full `pnpm test` (~17.7GB RAM); run targeted single-file vitest only.
- **Verify-not-assume:** the source audit over-flagged once (claimed a routine-trigger P0 that does not exist; claimed schema fields missing that are present). Before each task, confirm the anchor against real code in the worktree.

**Worktree:** `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/teams-parity` (branch `feat/teams-parity`, off `origin/main` `5eaafe34`).

---

## PHASE 1 — Hide task (unblocks the prod task-cleanup)

### Task 1: BFF — accept `hidden_at` on PatchIssueBody

**Files:**
- Modify: `apps/backend/routers/teams/schemas.py` (class `PatchIssueBody`, lines 67–79)
- Test: `apps/backend/tests/unit/routers/teams/test_work.py` (or the issues-patch test module — locate the existing `patch_issue` test; if none, add to `test_work.py`'s sibling for issues)

**Interfaces:**
- Produces: `PatchIssueBody.hidden_at: Optional[str]` → emits camelCase `hiddenAt` on the wire. Upstream `PATCH /api/issues/:id` reads `hiddenAt` (ISO string or null) and filters `hiddenAt IS NOT NULL` out of the issues list.

- [ ] **Step 1:** Write a failing test asserting `PatchIssueBody(hidden_at="2026-06-24T00:00:00Z").model_dump(by_alias=True, exclude_unset=True) == {"hiddenAt": "2026-06-24T00:00:00Z"}` and that passing `hidden_at=None` with `exclude_unset` still emits the key (so a task can be un-hidden).
- [ ] **Step 2:** Run it; expect FAIL (`extra_forbidden` / missing field).
- [ ] **Step 3:** Add to `PatchIssueBody`:
  ```python
  # Soft-hide: upstream PATCH /api/issues/:id accepts hiddenAt (ISO string)
  # and excludes hiddenAt-set rows from the issues list. Set to hide, null to
  # restore. The list endpoint already drops hidden rows upstream.
  hidden_at: Optional[str] = Field(default=None, max_length=40)
  ```
- [ ] **Step 4:** Run the test; expect PASS. Run `uv run ruff check . && uv run ruff format --check .` in `apps/backend`.
- [ ] **Step 5:** Commit `feat(teams): accept hiddenAt on issue PATCH for hide/restore`.

### Task 2: Frontend — Hide action in the task-detail "…" menu + IssueUpdateInput field

**Files:**
- Modify: `apps/frontend/src/components/teams/shared/types.ts` (`IssueUpdateInput`, lines 124–140 — add `hiddenAt?: string | null`)
- Modify: `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx` (the "…" `DropdownMenuContent`, lines 148–159 — add a Hide item above Delete; wire a `handleHide` that calls `mutations.update(issueId, { hiddenAt: new Date().toISOString() })` then `router.push("/teams")` — mirror `confirmDelete` at lines 54–64 but no confirm dialog)
- Test: `apps/frontend/src/components/teams/issues/__tests__/IssueDetailPage.hide.test.tsx` (new)

**Interfaces:**
- Consumes: `useIssueMutations().update(issueId, { hiddenAt })` (already exists, `useIssueMutations.ts:54–63`).

- [ ] **Step 1:** Write a failing vitest: render `IssueDetailPage` with a mocked `useIssueMutations` whose `update` is a spy; open the "…" menu; click "Hide task"; assert `update` called with `(issueId, { hiddenAt: <ISO string> })`. Annotate the mock builder's return type as `ReturnType<typeof useIssueMutations>` so tsc-over-tests passes.
- [ ] **Step 2:** Run `pnpm --filter @isol8/frontend exec vitest run <file>`; expect FAIL.
- [ ] **Step 3:** Add the menu item (use `EyeOff` from lucide):
  ```tsx
  <DropdownMenuItem
    onSelect={(e) => { e.preventDefault(); void handleHide(); }}
  >
    <EyeOff className="h-4 w-4" />
    Hide task
  </DropdownMenuItem>
  ```
  and `handleHide` mirroring `confirmDelete` (no confirm dialog — hide is reversible). Add `EyeOff` to the lucide import.
- [ ] **Step 4:** Run vitest; expect PASS. Run `pnpm --filter @isol8/frontend exec tsc --noEmit`.
- [ ] **Step 5:** Commit `feat(teams): hide task from the detail … menu`.

### Task 3: Frontend — Hide action on the Tasks-list row (so users hide without opening detail)

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/TasksList.tsx` (`TaskRow`, lines 383–497 — add a row-level "…" menu OR a hover Hide affordance calling `onUpdateIssue(issue.id, { hiddenAt: new Date().toISOString() })`)
- Test: extend `apps/frontend/src/components/teams/issues/__tests__/` for TasksList hide.

**Interfaces:**
- Consumes: `onUpdateIssue(id, { hiddenAt })` (TasksList already threads `onUpdateIssue` → `useIssueMutations.update`; `hiddenAt` added to `IssueUpdateInput` in Task 2).

- [ ] **Step 1:** Failing test: render `TasksList` with a spy `onUpdateIssue`; trigger the row Hide; assert `(id, { hiddenAt })`.
- [ ] **Step 2:** Run vitest; FAIL.
- [ ] **Step 3:** Add a compact row "…" `DropdownMenu` (right-aligned, appears on hover, theme tokens) with a single "Hide" item. Keep the row's `next/link` nav intact (stop propagation on the menu).
- [ ] **Step 4:** vitest PASS; `tsc --noEmit`.
- [ ] **Step 5:** Commit `feat(teams): hide task from the Tasks-list row menu`.

> After Phase 1 merges in the single PR, hidden tasks drop from the list. (The live prod cleanup of the 438 blocked/cancelled tasks runs post-merge by PATCHing `hiddenAt` in bulk via the page session — that's an operational step, not a code task.)

---

## PHASE 2 — Task-detail polish (one logical group)

### Task 4: Compact header + move Status/Priority editing into the Properties rail

This resolves the user-flagged duplication: today Status/Priority/Assignee are interactive in `IssueHeader` AND repeated in the rail (Status/Priority read-only, Assignee editable). Align to upstream — header keeps title + identifier + project + "…"; the rail owns all field editing.

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/IssueHeader.tsx` (remove the Status/Priority/Assignee inline pickers, lines ~136–158; keep the editable title at lines 95–134 and the identifier; keep/move the project chip). Drop now-unused `onStatusChange`/`onPriorityChange`/`onAssigneeChange` props from `IssueHeaderProps`.
- Modify: `apps/frontend/src/components/teams/issues/IssueProperties.tsx` (make Status row editable — pass `onChange` to `StatusIcon` at lines 86–91; make Priority row editable at lines 92–103; add `onStatusChange`/`onPriorityChange` to `IssuePropertiesProps` lines 25–35)
- Modify: `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx` (lines 120–135 + 197–210 — stop passing the three onChange to `IssueHeader`; pass `onStatusChange`/`onPriorityChange` to `IssueProperties`)
- Test: `apps/frontend/src/components/teams/issues/__tests__/IssueProperties.editable.test.tsx`

**Design review (frontend-design + interface-design):** header must read as a compact editorial masthead (title prominent, identifier muted mono, project as a subtle chip); the rail is the single editing surface. Run the swap/squint/token checks before marking done.

- [ ] **Step 1:** Failing test: render `IssueProperties` with `onStatusChange`/`onPriorityChange` spies; change Status and Priority via their pickers; assert the spies fire. Annotate mock return types.
- [ ] **Step 2:** vitest FAIL.
- [ ] **Step 3:** Implement: rail Status/Priority become interactive `StatusIcon`/`PriorityIcon` (mirror their header `onChange` usage); header loses the three pickers; `IssueDetailPage` rewires props.
- [ ] **Step 4:** vitest PASS; `tsc --noEmit`.
- [ ] **Step 5:** Commit `refactor(teams): single-source task fields in Properties rail, compact header`.

### Task 5: Add missing read-only Properties rows — Parent, Related Tasks, Sub-tasks count

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/IssueProperties.tsx` (add rows; Labels row already exists at 119–141 — move it up to sit directly after Priority per upstream order)
- Modify: `apps/frontend/src/components/teams/shared/types.ts` (`Issue` — add optional `relatedWork?` / `referencedIssueIdentifiers?` if upstream provides them; verify upstream payload first via the BFF before adding the type — do NOT add fields the wire doesn't carry)
- Test: `IssueProperties` row-render test.

**Interfaces:** Parent — reuse the data the existing `IssueParentBreadcrumb` fetches (`read<Issue>('/issues/{parentId}')`) but render as a labeled `PropertyRow "Parent"` chip linking to the parent. Sub-tasks — count via the existing `/issues/{id}/children` read; render `PropertyRow "Sub-tasks"` with the count (read-only). Related Tasks — only if `relatedWork`/`referenced*` is actually on the wire; otherwise SKIP and note it (no fabricated empty row).

- [ ] **Step 1:** Confirm via a quick BFF probe (or upstream `issues.ts` read) whether `relatedWork`/`referencedIssueIdentifiers` are in the `GET /issues/{id}` payload. Record the answer in the ledger.
- [ ] **Step 2:** Failing test for the Parent + Sub-tasks rows.
- [ ] **Step 3:** Implement the rows (theme `PropertyRow`); reorder Labels under Priority.
- [ ] **Step 4:** vitest PASS; `tsc --noEmit`.
- [ ] **Step 5:** Commit `feat(teams): add Parent / Sub-tasks (+Related) rows to task Properties`.

### Task 6: Inline description editing + Archive + Copy-as-markdown in the detail "…" menu

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx` (description block lines 162–172 → editable, mirroring the `IssueHeader` title-edit pattern: click-to-edit `Textarea`, save on blur via `mutations.update(issueId, { description })`; add "Archive" + "Copy as markdown" items to the "…" menu)
- Modify: `apps/frontend/src/components/teams/issues/hooks/useIssueMutations.ts` (add `archive(issueId)` → `post('/inbox/{id}/archive', {})`, reusing the existing BFF route confirmed at `routers/teams/issues.py:160`)
- Test: description-edit + archive + copy tests.

- [ ] **Step 1:** Failing tests: (a) editing description calls `update(id, { description })`; (b) Archive calls the archive endpoint; (c) Copy writes `# {identifier}: {title}\n\n{description}` to a mocked clipboard.
- [ ] **Step 2:** vitest FAIL.
- [ ] **Step 3:** Implement all three.
- [ ] **Step 4:** vitest PASS; `tsc --noEmit`.
- [ ] **Step 5:** Commit `feat(teams): inline description edit, archive, copy-as-markdown on task detail`.

---

## PHASE 3 — New-task composer parity

### Task 7: BFF — `work_mode` + `status` on CreateIssueBody; `work_mode` on PatchIssueBody

**Files:**
- Modify: `apps/backend/routers/teams/schemas.py` (`CreateIssueBody` 59–64, `PatchIssueBody` 67–79)
- Test: schema dump tests.

**Interfaces:** Produces `CreateIssueBody.work_mode` (`^(standard|ask|planning)$`) → `workMode`; `CreateIssueBody.status` (constrained to upstream statuses) → `status`; `PatchIssueBody.work_mode`. Confirm the upstream `createIssueSchema` accepts `workMode`/`status` before widening.

- [ ] **Step 1:** Confirm upstream `createIssueSchema` field names (`paperclip/.../validators`). Record in ledger.
- [ ] **Step 2:** Failing tests for the new fields' camelCase emission.
- [ ] **Step 3:** Add fields with strict patterns.
- [ ] **Step 4:** Tests PASS; ruff.
- [ ] **Step 5:** Commit `feat(teams): accept workMode + status on issue create`.

### Task 8: Composer UI — work-mode control, status preset, "task" terminology, field order

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx` (title "New task"/"New sub-task" line 116; button "Create task"/"Creating…" line 133; placeholder "Task title" line 155; add a work-mode segmented control + a status `<select>` preset; reorder Assignee before Project per upstream)
- Modify: `apps/frontend/src/components/teams/shared/types.ts` (`IssueCreateInput` 115–121 — add `workMode?`, `status?`)
- Test: composer test asserting payload includes `workMode`/`status` and copy says "task".

**Design review:** segmented control must use theme tokens; keep the dialog calm and uncluttered (interface-design density check).

- [ ] **Step 1:** Failing test: submit composer with a chosen work mode + status; assert payload keys + "Create task" label.
- [ ] **Step 2:** vitest FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** vitest PASS; `tsc --noEmit`.
- [ ] **Step 5:** Commit `feat(teams): richer New-task composer (work mode, status preset, task terminology)`.

---

## PHASE 4 — Tasks list/board refactor (mostly wiring already-ported parts)

### Task 9: Swap TaskRow → ported rich row + trailing columns + column picker

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/TasksList.tsx` (replace local `TaskRow` 383–497 with `inbox/IssueRow` + `InboxIssueTrailingColumns` from `inbox/IssueColumns.tsx:243`; wire `IssueColumnPicker` from `inbox/IssueColumns.tsx:91` into the toolbar 586–634; persist visible columns; keep inline status edit + add inline assignee via the trailing `assigneeContent` popover)
- Modify: `apps/frontend/src/components/teams/panels/IssuesPanel.tsx` + `projects/ProjectDetailPage.tsx` (feed `liveIssueIds` from the same source the inbox uses — `dashboard/hooks/useLiveRuns` — so the board "Live" badge lights up)
- Test: TasksList renders assignee/project/labels columns; column picker toggles; live ids reach the board.

**Design review:** column density + alignment (interface-design layering/squint checks); the row must not feel busier than the inbox.

- [ ] **Step 1:** Failing tests for the new columns + column-picker toggle + liveIssueIds plumbing.
- [ ] **Step 2:** vitest FAIL.
- [ ] **Step 3:** Implement the swap + wiring.
- [ ] **Step 4:** vitest PASS; `tsc --noEmit`.
- [ ] **Step 5:** Commit `refactor(teams): rich Tasks rows (columns, inline assignee, live indicators)`.

### Task 10: Wire filters popover + group/sort additions + empty states

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/TasksList.tsx` (wire `inbox/IssueFiltersPopover` into the toolbar; the BFF `GET /issues` already accepts `q`/`status`/`project`/`assignee`/`sortField`/`sortDir` per `issues.py:28–78`; add `priority` + `project` + `parent` to `GROUP_OPTIONS` 170–174 and `priority` to `SORT_OPTIONS` 163–168; add a filtered-empty state distinct from "No tasks yet"; dedupe the masthead vs in-list "New task" / search)
- Test: filter popover applies; group-by priority groups; filtered-empty distinct copy.

- [ ] **Step 1:** Failing tests.
- [ ] **Step 2:** vitest FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** vitest PASS; `tsc --noEmit`.
- [ ] **Step 5:** Commit `feat(teams): Tasks filters, group/sort parity, distinct empty states`.

---

## PHASE 5 — Single-field property features + Goals/Projects/Dashboard

> Each is one BFF whitelist field (or already present) + one rail picker / surface. Pin exact anchors at execution.

### Task 11: Parent reparent + human-user assignee + labels editing on task detail
- BFF: add `parent_id`, `assignee_user_id`, `label_ids` to `PatchIssueBody` (verify each against upstream `updateIssueSchema`). Enforce agent⊕user assignee mutual-exclusion client-side.
- UI: Parent picker (rail), human-user option in `AssigneePicker` (feed `members`), interactive Labels picker (label list at `/issues/labels` already proxied).
- Tests per field; commit `feat(teams): parent / human-assignee / labels editing on task detail`.

### Task 12: Related Tasks read-only chip row (if on the wire)
- Only if Task 5's probe confirmed `relatedWork`. Render chips. Commit `feat(teams): related-tasks chip row`.

### Task 13: Goals create level/status + Projects status + Project↔Goal link + Dashboard recent tasks
- BFF: `CreateGoalBody` += `level`/`status` (PatchGoalBody already has them, schemas.py 155–164); `PatchProjectBody` += `status` (verify upstream `updateProjectSchema`; note the existing budget comment 172–180 is about a different field); `goal_ids` link on project patch if upstream supports.
- UI: goal create pickers; project status picker; project↔goal link picker; Dashboard "Recent Tasks" column (panel already fetches `/issues`).
- Tests; commit `feat(teams): goal level/status, project status, project↔goal link, dashboard recent tasks`.

---

## PHASE 6 — Routines / Inbox usability

> Pin anchors at execution. The routine create/toggle schema is ALREADY correct ({title,assignee,status} + separate triggers) — do NOT "fix" it.

### Task 14: Routine trigger edit/delete (schedule-only) + run-now variables
- BFF: add `PATCH`/`DELETE /routines/{id}/triggers/{trigger_id}` proxy + admin-client methods (verify upstream routes); thread `variables` into run-now + create/patch routine bodies if upstream accepts.
- UI: editable Schedule section; Variables editor for run-now.
- Tests; commit `feat(teams): editable routine triggers + run-now variables`.

### Task 15: Join-requests surfaced in Inbox + failed-run Retry
- BFF: `list_join_requests` / `reject_join_request` admin methods + routes; `wakeup_agent` → `POST .../agents/{id}/wakeup` + route (verify upstream).
- UI: render the join-requests rows + approve/reject; add Retry to failed-run rows; wire/remove the dead `approvals`/`runs`/`joins` inbox tab references.
- Tests; commit `feat(teams): inbox join-requests + failed-run retry`.

---

## PHASE 7 — executionPolicy cluster: Reviewers, Approvers, Monitor

> The heaviest. Pin anchors at execution. Reviewers/Approvers/Monitor share one `execution_policy: Optional[dict]` whitelist field; Monitor adds a check-now route.

### Task 16: BFF — execution_policy passthrough + monitor check-now route
- BFF: `PatchIssueBody.execution_policy: Optional[dict]` (forwarded verbatim — it carries `monitor{nextCheckAt,notes,kind,serviceName}` + reviewer/approver stages; verify upstream `updateIssueSchema` shape); new `POST /issues/{id}/monitor/check-now` + `PaperclipAdminClient.monitor_check_now` (verify upstream route).
- Model `executionPolicy`/`monitor*` on the `Issue` type (only fields actually on the wire).
- Tests; commit `feat(teams): execution-policy passthrough + monitor check-now route`.

### Task 17: UI — Reviewers / Approvers pickers + Monitor row (re-check time, notes, external service, Check-now)
- UI: rail rows for Reviewers + Approvers (agents+members multi-select), Monitor row with `datetime-local` + notes + **External service** text field + Schedule/Clear + **Check now**. Port upstream's `buildExecutionPolicy` shape.
- Design review (frontend-design + interface-design) — this is a dense rail addition; layering/squint checks mandatory.
- Tests; commit `feat(teams): reviewers, approvers, and monitor scheduling on task detail`.

---

## Deferred (NOT in this PR — surfaced, not silently dropped)

- **Attachments** (upload/list/delete) — needs net-new BFF routes + storage audit.
- **Model/adapter lane** in composer — adapter-gated; hidden for our `openclaw_gateway` seed agents, so no visible parity gap.
- **Org-chart touch pan/pinch-zoom** — desktop-first; genuine but low-priority interaction gap.
- **List windowing / infinite scroll, board density controls** — perf items, matter only at high card counts.
- **Typed approval-payload cards, full inbox work-item interleave** — larger inbox rework.

---

## Final steps (after all tasks land on the branch)

1. **Whole-branch design review** — run frontend-design + interface-design skills over the full diff (theme discipline, swap/squint/signature/token checks, missing states).
2. **Final whole-branch code review** (subagent-driven-development's final reviewer).
3. Open **ONE PR**; let `claude-code-review.yml` CI run; address findings; merge once (gate on the actual backend + frontend CI checks).
4. **Live verify in claude-in-chrome on prod**: Hide (single + then bulk-clear the 438), compact header / rail editing, new rows, composer, Tasks columns/filters, routines, monitor.
