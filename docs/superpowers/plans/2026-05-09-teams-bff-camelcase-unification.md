# Teams BFF camelCase Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify the Teams BFF wire format on camelCase end-to-end (frontend ↔ FastAPI BFF ↔ Paperclip), so the issue-assignment bug — and every other latent snake/camel mismatch in the BFF — is foreclosed in one shot.

**Architecture:** Pydantic v2's `alias_generator=to_camel` + `populate_by_name=True` lets the `_Strict` base accept incoming JSON in either case while dumping camelCase via `by_alias=True`. The Teams `_Strict` body schemas all inherit from this base, so a single line on the base class cascades to every BFF body. Each `model_dump(...)` site is updated to pass `by_alias=True` so outbound bodies match Paperclip's wire. Frontend mutations stop translating to snake_case before sending; frontend response readers stop reading snake_case keys that Paperclip never produced.

**Tech Stack:** Pydantic 2.7+ (`pydantic.alias_generators.to_camel`), FastAPI, TypeScript / React, SWR.

---

## Bug background (read before starting)

The Teams BFF currently has a silent contract bug: it speaks snake_case on the wire to Paperclip, but Paperclip's REST API expects camelCase. Paperclip silently ignores unknown keys and returns the unchanged record, so:
- `PATCH /api/v1/teams/issues/{id}` with `{"assignee_agent_id": "agent_xyz"}` returns the issue unchanged — assignment never persists. (User-visible symptom: AssigneePicker click leaves issue as "Unassigned".)
- `GET /api/v1/teams/issues` returns Paperclip's camelCase response verbatim, so frontend code reading `issue.project_id` / `issue.assignee_agent_id` (e.g., `IssuesPanel.tsx:25-26`) reads `undefined` — issue rows render with no assignee or project even when one exists upstream.
- Same class of bug latent across `agents`, `settings`, `work` (routines/goals/projects).

Existing BFF tests (e.g., `test_issues.py::test_patch_issue_forwards_explicit_null_to_unassign`) stubbed `_admin().patch_issue` and asserted the *forwarded* body shape — so they froze the wrong contract. Those tests need to flip to expect camelCase.

## Files touched

**Backend:**
- Modify: `apps/backend/routers/teams/schemas.py` — `_Strict` base config
- Modify: `apps/backend/routers/teams/issues.py` — 3 model_dump calls
- Modify: `apps/backend/routers/teams/agents.py` — 1 model_dump call (line 415)
- Modify: `apps/backend/routers/teams/settings.py` — 1 model_dump call (line 36)
- Modify: `apps/backend/routers/teams/work.py` — 6 model_dump calls
- Modify: `apps/backend/tests/unit/routers/teams/test_issues.py` — flip body-shape assertions to camelCase + add regression test
- Modify: `apps/backend/tests/unit/routers/teams/test_work.py` — flip body-shape assertions
- Modify: `apps/backend/tests/unit/routers/teams/test_settings.py` — flip body-shape assertions
- Modify: `apps/backend/tests/unit/routers/teams/test_schemas.py` — add base-config regression test

**Frontend:**
- Modify: `apps/frontend/src/components/teams/issues/hooks/useIssueMutations.ts` — drop `toBackendCreateBody` / `toBackendUpdateBody`; send camelCase directly
- Modify: `apps/frontend/src/components/teams/shared/types.ts` — update jsdoc on `IssueUpdateInput.projectId` / `assigneeAgentId`
- Modify: `apps/frontend/src/components/teams/panels/IssuesPanel.tsx` — replace local snake_case `Issue` interface with the shared camelCase `Issue` type
- Modify: `apps/frontend/src/components/teams/panels/SettingsPanel.tsx` — `display_name` → `displayName` (both PATCH body + response read)

**Out of scope (deliberately):** `CostsPanel` (no `/teams/costs` BFF endpoint exists; placeholder), `MembersPanel.email_via_clerk` (unclear source), agent body fields beyond what already works. We can address those once the affected pages are actively wired up.

---

### Task 1: Backend — `_Strict` base config (camel alias generator)

**Files:**
- Modify: `apps/backend/routers/teams/schemas.py:18-20`
- Modify: `apps/backend/tests/unit/routers/teams/test_schemas.py` (add new test)

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/routers/teams/test_schemas.py`:

```python
def test_strict_base_emits_camel_case_via_by_alias():
    """The _Strict base is the BFF↔Paperclip wire-shape contract: snake_case
    Python attributes, camelCase JSON keys. Pydantic must dump camelCase
    when by_alias=True is passed and accept either name on input."""
    from routers.teams.schemas import PatchIssueBody

    # Construction with snake_case attribute names (internal Python callers).
    body = PatchIssueBody(assignee_agent_id="agt_1", project_id="proj_1")
    dumped = body.model_dump(exclude_unset=True, by_alias=True)
    assert dumped == {"assigneeAgentId": "agt_1", "projectId": "proj_1"}


def test_strict_base_accepts_camel_case_input():
    """Frontend posts camelCase JSON; the BFF must parse it without 422."""
    from routers.teams.schemas import PatchIssueBody

    body = PatchIssueBody.model_validate({"assigneeAgentId": "agt_1"})
    assert body.assignee_agent_id == "agt_1"


def test_strict_base_still_rejects_unknown_fields():
    """alias_generator must not weaken extra='forbid' — adapterType
    smuggling must still 422."""
    import pytest
    from pydantic import ValidationError
    from routers.teams.schemas import PatchIssueBody

    with pytest.raises(ValidationError):
        PatchIssueBody.model_validate({"adapterType": "evil"})
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd apps/backend
uv run pytest tests/unit/routers/teams/test_schemas.py::test_strict_base_emits_camel_case_via_by_alias -v
```

Expected: FAIL — current `_Strict` has no alias generator, so `model_dump(by_alias=True)` returns snake_case keys (Pydantic uses the field name when no alias is set).

- [ ] **Step 3: Add alias_generator to `_Strict`**

In `apps/backend/routers/teams/schemas.py`, replace the `_Strict` definition:

```python
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Strict(BaseModel):
    """Base for Teams BFF request bodies.

    All Teams BFF schemas use snake_case Python attribute names but
    must emit camelCase JSON to match Paperclip's wire shape. Pydantic's
    ``alias_generator=to_camel`` derives a camelCase alias for every
    field; ``populate_by_name=True`` keeps the snake_case attribute
    names usable from internal Python callers (and from the existing
    test fixtures). ``extra="forbid"`` still rejects unknown keys —
    aliasing does not weaken the security posture.

    Outbound: every router that forwards a body to Paperclip must call
    ``model_dump(..., by_alias=True)`` so the camelCase aliases land on
    the wire.
    """

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/routers/teams/test_schemas.py -v
```

Expected: PASS for the three new tests + all existing schemas tests.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/teams/schemas.py apps/backend/tests/unit/routers/teams/test_schemas.py
git commit -m "feat(teams-bff): _Strict base emits camelCase via alias_generator

Pydantic's to_camel alias generator + populate_by_name=True lets every
Teams BFF body schema accept either snake_case or camelCase on input
and dump camelCase via by_alias=True. extra='forbid' is preserved.

This is the single-line foundation for the BFF→Paperclip wire-shape
unification — Task 2 flips every model_dump call site to use it."
```

---

### Task 2: Backend — flip every `model_dump` call site to `by_alias=True`

**Files:**
- Modify: `apps/backend/routers/teams/issues.py:73, 95, 171`
- Modify: `apps/backend/routers/teams/agents.py:415`
- Modify: `apps/backend/routers/teams/settings.py:36`
- Modify: `apps/backend/routers/teams/work.py:51, 65, 132, 146, 181, 195`
- Modify: `apps/backend/tests/unit/routers/teams/test_issues.py` (3 forwarded-body assertions)
- Modify: `apps/backend/tests/unit/routers/teams/test_work.py` (forwarded-body assertions)
- Modify: `apps/backend/tests/unit/routers/teams/test_settings.py` (forwarded-body assertion)

- [ ] **Step 1: Add the regression test that motivated this PR (`assigneeAgentId` lands on the wire)**

In `apps/backend/tests/unit/routers/teams/test_issues.py`, append below the existing `test_patch_issue_forwards_explicit_null_to_unassign`:

```python
@pytest.mark.asyncio
async def test_patch_issue_forwards_camelcase_assignee_to_paperclip():
    """Paperclip's PATCH /api/issues/:id reads req.body.assigneeAgentId
    (camelCase). Our schema attribute is snake_case for Python ergonomics
    but the wire MUST be camelCase. This is the regression test for the
    'AssigneePicker click leaves issue Unassigned' bug — without this,
    Paperclip silently drops the unknown snake_case key and returns the
    unchanged issue."""
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"

    body = PatchIssueBody(assignee_agent_id="agt_friday", project_id="proj_x")

    with patch("routers.teams.issues._agents", fake_agents_module):
        await patch_issue(issue_id="iss_1", body=body, ctx=fake_ctx)

    forwarded_body = fake_admin.patch_issue.call_args.kwargs["body"]
    assert forwarded_body == {"assigneeAgentId": "agt_friday", "projectId": "proj_x"}
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
cd apps/backend
uv run pytest tests/unit/routers/teams/test_issues.py::test_patch_issue_forwards_camelcase_assignee_to_paperclip -v
```

Expected: FAIL — current call sites pass snake_case body to Paperclip.

- [ ] **Step 3: Add `by_alias=True` to all 11 `model_dump` call sites**

`apps/backend/routers/teams/issues.py:73`:

```python
        body=body.model_dump(exclude_none=True, by_alias=True),
```

`apps/backend/routers/teams/issues.py:95`:

```python
        body=body.model_dump(exclude_unset=True, by_alias=True),
```

`apps/backend/routers/teams/issues.py:171`:

```python
        body=body.model_dump(by_alias=True),
```

`apps/backend/routers/teams/agents.py:415`:

```python
    payload = body.model_dump(exclude_none=True, by_alias=True)
```

`apps/backend/routers/teams/settings.py:36`:

```python
    payload = body.model_dump(exclude_none=True, by_alias=True)
```

`apps/backend/routers/teams/work.py` — for **each** of lines 51, 65, 132, 146, 181, 195, replace `model_dump(exclude_none=True)` with `model_dump(exclude_none=True, by_alias=True)`. (Six call sites; the surrounding code is identical pattern in each — check that you've changed all six before committing.)

- [ ] **Step 4: Run the new regression test to verify it now passes**

```bash
uv run pytest tests/unit/routers/teams/test_issues.py::test_patch_issue_forwards_camelcase_assignee_to_paperclip -v
```

Expected: PASS.

- [ ] **Step 5: Update existing forwarded-body assertions to camelCase**

In `apps/backend/tests/unit/routers/teams/test_issues.py`, update:

- `test_patch_issue_forwards_explicit_null_to_unassign` — line 301:
  ```python
  assert forwarded_body == {"assigneeAgentId": None}
  ```
  Update the docstring accordingly:
  ```python
  """An admin sending {"assigneeAgentId": null} (or its snake_case Python
  equivalent) must reach Paperclip as null, not be stripped. Otherwise
  users can't unassign."""
  ```

- `test_patch_issue_omits_unset_fields` — no change needed (asserts `{}`, which is shape-agnostic).

- [ ] **Step 6: Update test_work.py forwarded-body assertions**

Open `apps/backend/tests/unit/routers/teams/test_work.py`. For every test that asserts the body forwarded to a Paperclip create/update method (routines, goals, projects), check whether the asserted dict contains snake_case keys. If yes, flip them to camelCase. Run after each edit:

```bash
uv run pytest tests/unit/routers/teams/test_work.py -v
```

(If a test only asserts single-word fields like `name`, `enabled`, `cron`, no change needed.)

- [ ] **Step 7: Update test_settings.py forwarded-body assertions**

Same drill as Step 6 for `apps/backend/tests/unit/routers/teams/test_settings.py`. The settings PATCH body uses `display_name` / `description`. Update any asserted dict that contains `display_name` to assert `displayName` instead.

```bash
uv run pytest tests/unit/routers/teams/test_settings.py -v
```

- [ ] **Step 8: Run the full Teams BFF test suite**

```bash
uv run pytest tests/unit/routers/teams/ -v
```

Expected: PASS for every test in the directory.

- [ ] **Step 9: Commit**

```bash
git add apps/backend/routers/teams/ apps/backend/tests/unit/routers/teams/
git commit -m "feat(teams-bff): forward camelCase bodies to Paperclip

All 11 model_dump call sites in the Teams BFF now pass by_alias=True so
outbound payloads match Paperclip's wire (camelCase). Closes the
silent assignee/project drop where Paperclip ignored unknown
snake_case keys and returned issues unchanged.

Adds a regression test pinning {assigneeAgentId, projectId} on the
PATCH /api/v1/teams/issues/{id} forwarded body."
```

---

### Task 3: Frontend — drop snake conversions; fix snake response reads

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/hooks/useIssueMutations.ts`
- Modify: `apps/frontend/src/components/teams/shared/types.ts` (jsdoc only)
- Modify: `apps/frontend/src/components/teams/panels/IssuesPanel.tsx`
- Modify: `apps/frontend/src/components/teams/panels/SettingsPanel.tsx`

- [ ] **Step 1: Drop the snake-case body builders in useIssueMutations**

In `apps/frontend/src/components/teams/issues/hooks/useIssueMutations.ts`:

- Delete `toBackendCreateBody` (currently lines ~28-39) entirely.
- Delete `toBackendUpdateBody` (currently lines ~41-54) entirely.
- Update the create call site to pass the camelCase input directly:
  ```ts
  const created = await post<Issue>("/issues", input as unknown as Record<string, unknown>);
  ```
  (or, cleaner: drop the `Record<string, unknown>` indirection if the `post` signature accepts the typed input)
- Update the update call site:
  ```ts
  const updated = await patch<Issue>(`/issues/${issueId}`, input as unknown as Record<string, unknown>);
  ```
- Remove the now-stale comment block above `toBackendCreateBody` referring to "title / description / project_id / assignee_agent_id".

- [ ] **Step 2: Refresh jsdoc on `IssueUpdateInput`**

In `apps/frontend/src/components/teams/shared/types.ts`, update the jsdoc on `projectId` and `assigneeAgentId`:

```ts
  /** ``null`` explicitly clears the project (sent as ``projectId: null`` in the
   * PATCH body). ``undefined`` leaves the project unchanged. */
  projectId?: string | null;
  /** ``null`` explicitly unassigns (sent as ``assigneeAgentId: null`` in the
   * PATCH body). ``undefined`` leaves the assignee unchanged. */
  assigneeAgentId?: string | null;
```

- [ ] **Step 3: Replace IssuesPanel's local Issue interface with the shared camelCase type**

In `apps/frontend/src/components/teams/panels/IssuesPanel.tsx`:

- Delete the local `interface Issue { ... project_id?: string; assignee_agent_id?: string; ... }` (currently lines ~20-28).
- Add `Issue` to the existing import from `@/components/teams/shared/types`:
  ```ts
  import type {
    CompanyAgent,
    Issue,
    IssueProject,
  } from "@/components/teams/shared/types";
  ```
- The shared `Issue` type already has `assigneeAgentId` (camelCase), so any panel code reading `issue.project_id` / `issue.assignee_agent_id` must now read `issue.project` / `issue.assigneeAgentId`. (The shared type has `project?: IssueProject | null` rather than `projectId?` — verify the rendering path matches.)

- [ ] **Step 4: SettingsPanel — `display_name` → `displayName` (PATCH body + response read)**

In `apps/frontend/src/components/teams/panels/SettingsPanel.tsx`:

- Line ~7: rename the local response-shape field `display_name?: string;` → `displayName?: string;`.
- Line ~21: update the read: `setName(data.displayName ?? "");`
- Line ~33: update the PATCH body: `await patch("/settings", { displayName: name, description: desc });`
- Update the SECURITY comment on line 30 to reflect the camelCase shape.

- [ ] **Step 5: Run frontend typecheck + lint**

```bash
cd apps/frontend
pnpm run lint
pnpm exec tsc --noEmit
```

Expected: PASS. If `tsc --noEmit` flags missing fields on `Issue` (e.g., the IssuesPanel was relying on `project_id` somewhere we missed), fix the read site to use the shared type's actual field (`issue.project?.id` etc.) before proceeding.

- [ ] **Step 6: Run frontend unit tests**

```bash
pnpm test -- --run --testPathPattern teams
```

Expected: PASS. If any frontend unit test asserted the old snake_case body shape sent to `post`/`patch`, update it to assert camelCase.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/teams/
git commit -m "feat(teams-frontend): speak camelCase to /teams BFF end-to-end

Drops the snake-case conversion in useIssueMutations: the BFF now
accepts camelCase aliases (Pydantic populate_by_name=True) and the
typed input shape is camelCase already, so the converter was just a
boundary translation that hid the upstream contract.

Fixes IssuesPanel's local Issue interface (which read non-existent
snake_case fields off Paperclip's camelCase response — assignee /
project chips never rendered). Switches SettingsPanel to displayName
both ways."
```

---

### Task 4: Verify end-to-end + cut PR

**Files:** none modified — verification + PR.

- [ ] **Step 1: Run the full backend test suite (not just Teams)**

```bash
cd apps/backend
uv run pytest tests/ -v
```

Expected: PASS. The schema base-class change touches `_Strict` which is the parent of every Teams BFF body — surface anything it accidentally broke elsewhere.

- [ ] **Step 2: Run the full frontend lint + typecheck**

```bash
cd apps/frontend
pnpm run lint
pnpm exec tsc --noEmit
```

Expected: PASS.

- [ ] **Step 3: Push branch + open PR**

```bash
git push -u origin fix/teams-bff-camelcase-issue-body
gh pr create --title "feat(teams): unify Teams BFF wire on camelCase end-to-end" --body "$(cat <<'EOF'
## Summary
- Pydantic ``_Strict`` base now uses ``alias_generator=to_camel`` + ``populate_by_name=True`` so every Teams BFF body schema dumps camelCase via ``by_alias=True`` and accepts either case on input.
- All 11 ``model_dump`` call sites in ``routers/teams/`` (issues, agents, settings, work) pass ``by_alias=True`` so outbound payloads match Paperclip's wire.
- Frontend drops the snake-case conversion in ``useIssueMutations`` (toBackendCreateBody / toBackendUpdateBody) and sends camelCase directly. ``IssuesPanel`` switches to the shared camelCase ``Issue`` type so picker chips actually render. ``SettingsPanel`` swaps ``display_name`` for ``displayName`` on both PATCH body and response read.

## Why
Paperclip's REST API reads camelCase keys (``assigneeAgentId``, ``projectId``, ``columnId``, ``displayName``) but the BFF was forwarding snake_case from our Pydantic schemas verbatim. Paperclip silently dropped the unknown keys and returned the unchanged record, so AssigneePicker / ProjectPicker clicks looked successful but never persisted. Same class of bug latent on agents / routines / goals / projects / settings.

## Test plan
- [ ] ``cd apps/backend && uv run pytest tests/ -v`` — full backend suite passes
- [ ] ``cd apps/frontend && pnpm run lint && pnpm exec tsc --noEmit`` — clean
- [ ] On dev: assign an issue to Friday via ``IssueProperties.AssigneePicker`` → refresh → assignment persists
- [ ] On dev: change project via ``IssueProperties.ProjectPicker`` → refresh → project persists
- [ ] On dev: edit company display name via ``/teams/settings`` → refresh → name persists

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Watch CI**

```bash
gh run watch <run-id> --repo Isol8AI/isol8 --exit-status
```

Expected: PASS. If frontend / backend / e2e gate fails, address the failure before requesting review.

---

## Self-review

**Spec coverage:**
- ✅ BFF schemas alias to camelCase (Task 1)
- ✅ All model_dump call sites flipped (Task 2)
- ✅ Frontend stops sending snake_case to BFF (Task 3)
- ✅ Frontend reads correct camelCase from BFF responses (Task 3)
- ✅ Existing tests fixed, regression test added (Task 2)
- ✅ Full verification + PR (Task 4)

**Type consistency:** Field names referenced consistently — `assigneeAgentId`, `projectId`, `columnId`, `displayName`. Python attribute names stay snake_case (`assignee_agent_id`).

**Out of scope deliberately:** CostsPanel and MembersPanel reads — their endpoint sources are unclear (no `/teams/costs` BFF route exists; CostsPanel is a placeholder). Document as follow-up if those pages become active.
