# Onboarding Goal + Project Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a company-level goal and a project at brief-submit so Friday's first-run tasks are attributed to something, and let her refine both during her session.

**Architecture:** Two Paperclip rows are created synchronously inside `POST /api/v1/onboarding/brief`, before the instruction files are staged onto EFS, so Friday can never wake into a company with no project. A DynamoDB sentinel row keyed on `company_id` makes the seed one-shot per company rather than per user, which is what makes orgs safe. Friday renames the company and project as her first visible action and rewrites the goal after research.

**Tech Stack:** FastAPI, DynamoDB (boto3), pytest + moto, Paperclip REST via `paperclip_admin_client`.

**Spec:** `docs/superpowers/specs/2026-07-19-onboarding-goal-and-project-design.md`

## Global Constraints

- The goal MUST be created with `level="company"`, `status="active"`, and no `parentId`. `getDefaultCompanyGoal` selects the oldest active root goal; any deviation means it is never selected.
- The goal MUST be created **before** the project. The project create passes `goalIds: [goal_id]`, and Paperclip mirrors `goalIds[0]` into the legacy `projects.goalId` column that `getProjectDefaultGoalId` reads.
- The project MUST NOT set `targetDate`. It is a permanent bucket, not a milestone.
- Project name is `f"{first_name}'s Company"` on every path. Never a placeholder string.
- Goal title is a real mission statement on every path. Never a placeholder string.
- Existing behaviour that must not regress: `BRIEF.md` stays `write_file_if_missing`, `BOOTSTRAP.md` stays an unconditional overwrite, and a replay still returns `{"status": "already_submitted"}`.
- Branch: `spec/onboarding-goal-and-project`. Do not push without explicit approval.
- Run backend tests with `cd apps/backend && uv run pytest <path> -v`.

---

### Task 1: Onboarding sentinel in `paperclip_repo`

The `paperclip-companies` table is keyed on `user_id` — one row per user, with `company_id` repeated across an org's rows. There is no per-company row, so the one-shot marker is a sentinel row with a synthetic key, modelled on `_ORG_CREATE_CLAIM_PREFIX` at `paperclip_repo.py:63`.

Unlike the org-create claim, this one has **no stale-stealing**: onboarding is one-shot per company forever, and failures release the claim explicitly rather than timing out.

**Files:**
- Modify: `apps/backend/core/repositories/paperclip_repo.py` (add after `_org_create_claim_key`, and methods near the existing claim methods at :378 and :443)
- Test: `apps/backend/tests/unit/repositories/test_paperclip_repo_onboarding.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `claim_onboarding(company_id: str) -> bool` — True iff this caller won the claim
  - `release_onboarding_claim(company_id: str) -> None`
  - `record_onboarding_ids(company_id: str, *, goal_id: str, project_id: str) -> None`
  - `get_onboarding(company_id: str) -> Optional[dict]`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/repositories/test_paperclip_repo_onboarding.py`. The fixture mirrors `test_container_repo_provision_claim.py:27-40` — a real moto table, not a mock, because every primitive here is a conditional write and a mock would happily accept expressions DynamoDB rejects.

Note the prefix indirection: `PaperclipRepo._table()` resolves through `core.dynamodb.get_table`, so the table is created as `test-paperclip-companies` while the repo is constructed with `"paperclip-companies"`.

```python
"""Onboarding sentinel: one-shot per COMPANY, not per user.

Exercised against a REAL DynamoDB engine (moto) rather than a hand mock —
the claim is a conditional write and a mock cannot tell us whether
DynamoDB would actually reject the second one.
"""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core.repositories.paperclip_repo import PaperclipRepo

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def paperclip_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-paperclip-companies",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(
            TableName="test-paperclip-companies"
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield "paperclip-companies"


@pytest.mark.asyncio
async def test_first_claim_wins_second_loses(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    assert await repo.claim_onboarding("comp-1") is False


@pytest.mark.asyncio
async def test_claim_is_scoped_per_company(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    assert await repo.claim_onboarding("comp-2") is True


@pytest.mark.asyncio
async def test_release_allows_reclaim(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    await repo.release_onboarding_claim("comp-1")
    assert await repo.claim_onboarding("comp-1") is True


@pytest.mark.asyncio
async def test_record_ids_then_read_back(paperclip_table):
    repo = PaperclipRepo(paperclip_table)
    await repo.claim_onboarding("comp-1")

    await repo.record_onboarding_ids("comp-1", goal_id="goal-1", project_id="proj-1")

    row = await repo.get_onboarding("comp-1")
    assert row["onboarding_goal_id"] == "goal-1"
    assert row["onboarding_project_id"] == "proj-1"


@pytest.mark.asyncio
async def test_get_onboarding_absent_returns_none(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.get_onboarding("comp-nope") is None


@pytest.mark.asyncio
async def test_sentinel_does_not_collide_with_a_user_row(paperclip_table):
    """The synthetic key must not be reachable as a real user_id."""
    repo = PaperclipRepo(paperclip_table)
    await repo.claim_onboarding("comp-1")

    assert await repo.get("comp-1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_paperclip_repo_onboarding.py -v`
Expected: FAIL with `AttributeError: 'PaperclipRepo' object has no attribute 'claim_onboarding'`

- [ ] **Step 3: Write the implementation**

Add next to `_org_create_claim_key` (`paperclip_repo.py:63-67`):

```python
_ONBOARDING_CLAIM_PREFIX = "__onboarded__:"


def _onboarding_claim_key(company_id: str) -> str:
    return f"{_ONBOARDING_CLAIM_PREFIX}{company_id}"
```

Add these methods to `PaperclipRepo`, alongside the org-claim methods:

```python
    async def claim_onboarding(self, company_id: str) -> bool:
        """Claim the one-shot onboarding seed for a COMPANY.

        The table is keyed per-user, so a field on the submitting user's
        row would leave every other member of the same org unmarked. This
        sentinel is keyed on company_id instead, following the same shape
        as the org-create claim above.

        PK conditional write: strongly consistent and cross-instance-safe,
        so two tabs submitting the brief at once cannot both seed. Exactly
        one gets True.

        Unlike the org-create claim there is no stale-stealing — onboarding
        happens once per company, forever. Callers MUST
        ``release_onboarding_claim`` on failure so a retry can re-enter.
        """
        table = self._table()
        try:
            await run_in_thread(
                table.put_item,
                Item={
                    "user_id": _onboarding_claim_key(company_id),
                    "kind": "onboarding_claim",
                    "company_id": company_id,
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                },
                ConditionExpression="attribute_not_exists(user_id)",
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def release_onboarding_claim(self, company_id: str) -> None:
        """Drop the claim so a failed seed can be retried."""
        await run_in_thread(
            self._table().delete_item,
            Key={"user_id": _onboarding_claim_key(company_id)},
        )

    async def record_onboarding_ids(
        self, company_id: str, *, goal_id: str, project_id: str
    ) -> None:
        """Record the seeded rows on the claim. Written LAST, so a claim
        carrying ids always means both Paperclip rows exist and are linked.
        """
        await run_in_thread(
            self._table().update_item,
            Key={"user_id": _onboarding_claim_key(company_id)},
            UpdateExpression=(
                "SET onboarding_goal_id = :g, onboarding_project_id = :p, seeded_at = :t"
            ),
            ExpressionAttributeValues={
                ":g": goal_id,
                ":p": project_id,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def get_onboarding(self, company_id: str) -> Optional[dict]:
        """Read the sentinel. Strongly consistent — callers branch on it."""
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": _onboarding_claim_key(company_id)},
            ConsistentRead=True,
        )
        return resp.get("Item")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_paperclip_repo_onboarding.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/paperclip_repo.py \
        apps/backend/tests/unit/repositories/test_paperclip_repo_onboarding.py
git commit -m "feat(onboarding): company-scoped onboarding claim sentinel"
```

---

### Task 2: `delete_project` on the client and the BFF

Paperclip has `DELETE /projects/:id` (`routes/projects.ts:677`); we never wired it. Without this a seeded project can never be removed, which is not acceptable for something we create on the user's behalf. Goals already have the full trio — model on `delete_goal` (`paperclip_admin_client.py:2453`) and the `delete_goal` route in `work.py`.

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (after `patch_project`)
- Modify: `apps/backend/routers/teams/work.py` (after `patch_project`, ~:559)
- Test: `apps/backend/tests/unit/routers/teams/test_work.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PaperclipAdminClient.delete_project(*, session_cookie: str, project_id: str) -> dict`; route `DELETE /api/v1/teams/projects/{project_id}`.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/routers/teams/test_work.py`, matching the fixtures already in that file:

```python
def test_delete_project_calls_admin(client):
    client.admin.delete_project = AsyncMock(return_value={"ok": True})

    resp = client.delete("/api/v1/teams/projects/proj-1")

    assert resp.status_code == 200
    client.admin.delete_project.assert_awaited_once_with(
        project_id="proj-1",
        session_cookie="cookie",
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_work.py::test_delete_project_calls_admin -v`
Expected: FAIL with 405 Method Not Allowed

- [ ] **Step 3: Write the implementation**

In `paperclip_admin_client.py`, after `patch_project`:

```python
    async def delete_project(
        self,
        *,
        session_cookie: str,
        project_id: str,
    ) -> dict:
        """Delete a project.

        Maps to ``DELETE /api/projects/{projectId}``. ``_delete`` treats 404
        as already-gone, so a double-delete from a stale UI is a no-op.
        """
        return await self._delete(
            f"/api/projects/{project_id}",
            session_cookie=session_cookie,
        )
```

In `routers/teams/work.py`, after `patch_project`:

```python
@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete a project.

    ``_delete`` treats a 404 as already-gone (idempotent), so a
    double-delete from a stale UI is a no-op rather than an error.
    """
    return await _agents._admin().delete_project(
        project_id=project_id,
        session_cookie=ctx.session_cookie,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_work.py -v`
Expected: all pass, including the new one

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/paperclip_admin_client.py \
        apps/backend/routers/teams/work.py \
        apps/backend/tests/unit/routers/teams/test_work.py
git commit -m "feat(teams): wire DELETE /projects/{id}"
```

---

### Task 3: Provisional titles and the seeding function

Pure functions for the provisional strings, plus the two-call seed with rollback. No router changes yet — this task is independently testable.

**Files:**
- Modify: `apps/backend/core/services/onboarding_brief.py`
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `derive_goal_title(*, path: str, user_input: str | None) -> str`
  - `project_name_for(first_name: str) -> str`
  - `seed_goal_and_project(*, admin, session_cookie: str, company_id: str, friday_agent_id: str, goal_title: str, project_name: str) -> tuple[str, str]` returning `(goal_id, project_id)`

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/services/test_onboarding_brief.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.onboarding_brief import (
    derive_goal_title,
    project_name_for,
    seed_goal_and_project,
)


def test_goal_title_new_path_uses_their_words():
    assert (
        derive_goal_title(path="new", user_input="A social media agency for restaurants")
        == "A social media agency for restaurants"
    )


def test_goal_title_new_path_truncates_long_input():
    title = derive_goal_title(path="new", user_input="x" * 300)
    assert len(title) <= 120
    assert title.endswith("…")


def test_goal_title_grow_path_is_a_mission_not_a_url():
    assert derive_goal_title(path="grow", user_input="https://www.ferrosocial.com/pricing?x=1") == (
        "Grow ferrosocial.com"
    )


def test_goal_title_no_idea_path_describes_the_actual_situation():
    assert derive_goal_title(path="new", user_input=None) == "Find the right business to build"


def test_project_name_is_never_a_placeholder():
    assert project_name_for("Max") == "Max's Company"


@pytest.mark.asyncio
async def test_seed_creates_goal_then_project_linked_to_it():
    admin = MagicMock()
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(return_value={"id": "proj-1"})

    goal_id, project_id = await seed_goal_and_project(
        admin=admin,
        session_cookie="cookie",
        company_id="comp-1",
        friday_agent_id="friday-uuid",
        goal_title="A social media agency",
        project_name="Max's Company",
    )

    assert (goal_id, project_id) == ("goal-1", "proj-1")

    goal_body = admin.create_goal.await_args.kwargs["body"]
    assert goal_body["level"] == "company"
    assert goal_body["status"] == "active"
    assert "parentId" not in goal_body

    project_body = admin.create_project.await_args.kwargs["body"]
    # goalIds[0] is mirrored into projects.goalId, which is what
    # getProjectDefaultGoalId reads for issue inheritance.
    assert project_body["goalIds"] == ["goal-1"]
    assert "targetDate" not in project_body


@pytest.mark.asyncio
async def test_seed_rolls_back_the_goal_when_project_create_fails():
    admin = MagicMock()
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(side_effect=RuntimeError("upstream down"))
    admin.delete_goal = AsyncMock()

    with pytest.raises(RuntimeError):
        await seed_goal_and_project(
            admin=admin,
            session_cookie="cookie",
            company_id="comp-1",
            friday_agent_id="friday-uuid",
            goal_title="A social media agency",
            project_name="Max's Company",
        )

    admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -v`
Expected: FAIL with `ImportError: cannot import name 'derive_goal_title'`

- [ ] **Step 3: Write the implementation**

Add to `onboarding_brief.py`:

```python
_MAX_GOAL_TITLE = 120


def _domain_of(raw: str) -> str:
    """Best-effort registrable domain from whatever went in the URL field.

    The grow-path input is free text — users type ``ferrosocial.com``,
    ``https://ferrosocial.com/pricing``, or ``www.ferrosocial.com``. Falls
    back to the raw string rather than raising: this feeds a provisional
    title Friday overwrites within one turn, so a scruffy value is a
    cosmetic problem, not a failure.
    """
    value = raw.strip()
    if "//" in value:
        value = value.split("//", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0].strip().lower()
    if value.startswith("www."):
        value = value[4:]
    return value or raw.strip()


def derive_goal_title(*, path: str, user_input: str | None) -> str:
    """Provisional goal title — a real mission statement on every path.

    Friday rewrites this after research. It must never read as filler in
    the meantime, because the user is watching the board while she works.
    """
    if not user_input:
        # "I don't know yet — you pick". This is an accurate description of
        # their situation and stops being true the moment Friday chooses.
        return "Find the right business to build"
    if path == "grow":
        return f"Grow {_domain_of(user_input)}"
    text = user_input.strip()
    if len(text) <= _MAX_GOAL_TITLE:
        return text
    return text[: _MAX_GOAL_TITLE - 1].rstrip() + "…"


def project_name_for(first_name: str) -> str:
    """The funnel never collects a company name, and the description makes a
    bad one ("A social media agency for small restaurants" is not a name).
    The owner's first name is always available and always reads as real.
    Friday renames it as her first visible action.
    """
    return f"{first_name}'s Company"


async def seed_goal_and_project(
    *,
    admin,
    session_cookie: str,
    company_id: str,
    friday_agent_id: str,
    goal_title: str,
    project_name: str,
) -> tuple[str, str]:
    """Create the company goal, then the project linked to it.

    Order is load-bearing. The project passes ``goalIds: [goal_id]``, and
    Paperclip mirrors ``goalIds[0]`` into the legacy ``projects.goalId``
    column that ``getProjectDefaultGoalId`` reads. A project with a null
    ``goalId`` is worse than no project at all: ``resolveIssueGoalId``
    returns ``projectGoalId ?? null`` for any issue that HAS a project and
    does not fall through to the company default, so every task filed into
    it would be silently unattributed.

    ``level="company"``, ``status="active"``, no ``parentId`` — anything
    else and ``getDefaultCompanyGoal`` never selects it.

    Rolls the goal back if the project create fails, so a retry never finds
    a half-seeded company.
    """
    goal = await admin.create_goal(
        session_cookie=session_cookie,
        company_id=company_id,
        body={
            "title": goal_title,
            "level": "company",
            "status": "active",
            "ownerAgentId": friday_agent_id,
        },
    )
    goal_id = goal["id"]
    try:
        project = await admin.create_project(
            session_cookie=session_cookie,
            company_id=company_id,
            body={
                "name": project_name,
                "status": "in_progress",
                "goalIds": [goal_id],
                "leadAgentId": friday_agent_id,
            },
        )
    except Exception:
        await admin.delete_goal(session_cookie=session_cookie, goal_id=goal_id)
        raise
    return goal_id, project["id"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -v`
Expected: all pass

- [ ] **Step 5: Verify the create-goal response shape**

The implementation reads `goal["id"]` and `project["id"]`. Confirm the BFF passes Paperclip's row through unwrapped rather than enveloping it:

Run: `grep -n "envelope" apps/backend/routers/teams/work.py | head`

If `create_goal`/`create_project` in `work.py` wrap the response, adjust the two subscripts to match and note it in the commit body. If they return the upstream dict directly (as `create_project` at `work.py:536-543` does), no change needed.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/onboarding_brief.py \
        apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): provisional titles + goal/project seeding"
```

---

### Task 4: Wire seeding into the brief route

Replace the two guards with the company-scoped claim, and seed before staging files.

**Files:**
- Modify: `apps/backend/routers/onboarding.py:70-133`
- Test: `apps/backend/tests/unit/routers/test_onboarding.py`

**Interfaces:**
- Consumes: `claim_onboarding` / `release_onboarding_claim` / `record_onboarding_ids` (Task 1); `derive_goal_title` / `project_name_for` / `seed_goal_and_project` (Task 3).
- Produces: response gains `goal_id` and `project_id` alongside the existing `friday_agent_id`.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/routers/test_onboarding.py`. Extend the existing `client` fixture so `admin.create_goal` / `admin.create_project` are stubbed and the repo methods are monkeypatched — follow the fixture style already in the file.

```python
def test_org_admin_is_no_longer_rejected(client, monkeypatch):
    app.dependency_overrides[get_current_user] = _org_auth

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "staged"


def test_seeds_goal_and_project_before_staging_files(client):
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["goal_id"] == "goal-1"
    assert body["project_id"] == "proj-1"
    client.admin.create_goal.assert_awaited_once()
    client.admin.create_project.assert_awaited_once()


def test_second_submit_for_same_company_is_a_noop(client):
    client.claim.return_value = False

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.json() == {"status": "already_submitted"}
    client.admin.create_goal.assert_not_awaited()
    client.ws.write_file_if_missing.assert_not_called()


def test_paperclip_not_ready_releases_the_claim(client):
    client.admin.list_agents = AsyncMock(side_effect=PaperclipApiError("boom"))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.release.assert_awaited_once_with("comp-1")


def test_seed_failure_releases_the_claim(client):
    client.admin.create_goal = AsyncMock(side_effect=RuntimeError("upstream down"))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.release.assert_awaited_once_with("comp-1")
    client.ws.write_file_if_missing.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_onboarding.py -v`
Expected: FAIL — `test_org_admin_is_no_longer_rejected` gets 403 `personal_only`

- [ ] **Step 3: Write the implementation**

Replace the guards at `routers/onboarding.py:76-83`. Delete the `billing_repo` import and the `auth.is_org_context` check entirely.

```python
    # One-shot per COMPANY, not per user: the paperclip-companies table is
    # keyed per-user, so an org's second member has their own unmarked row
    # and would otherwise be served the funnel and seed a duplicate pair.
    # A PK conditional write also means two tabs submitting at once cannot
    # both seed — exactly one wins.
    repo = PaperclipRepo(table_name="paperclip-companies")
    if not await repo.claim_onboarding(ctx.company_id):
        return {"status": "already_submitted"}
```

There is no shared accessor for this repo — every caller constructs it inline with
that exact literal (`ecs_manager.py:2584`, `account_wipe.py:193`, `agent_export.py:1056`).
Follow the established pattern rather than adding a getter.

Wrap everything after the claim so any failure releases it:

```python
    try:
        admin = _agents._admin()
        try:
            agents = await admin.list_agents(
                session_cookie=ctx.session_cookie, company_id=ctx.company_id
            )
        except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
            logger.warning(
                "onboarding: list_agents failed for %s -- Paperclip not ready: %s",
                ctx.owner_id,
                e,
            )
            raise HTTPException(status_code=409, detail="not_ready") from e
        friday = next((a for a in agents if a.get("role") == "ceo"), None)
        if friday is None:
            raise HTTPException(status_code=409, detail="not_ready")
        friday_id = friday["id"]

        first_name = _first_name(auth)

        # Seed BEFORE staging the instruction files: BOOTSTRAP.md tells
        # Friday to go to work, so the rows must exist before it lands or
        # she can wake into a company with no project.
        try:
            goal_id, project_id = await seed_goal_and_project(
                admin=admin,
                session_cookie=ctx.session_cookie,
                company_id=ctx.company_id,
                friday_agent_id=friday_id,
                goal_title=derive_goal_title(path=body.path, user_input=body.input),
                project_name=project_name_for(first_name),
            )
        except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
            logger.warning(
                "onboarding: seeding goal/project failed for %s: %s", ctx.owner_id, e
            )
            raise HTTPException(status_code=409, detail="not_ready") from e

        brief_md = render_brief_md(
            path=body.path,
            user_input=body.input,
            detail=body.detail,
            display_name=first_name,
            email=auth.email or "",
        )
        bootstrap_md = render_first_run_bootstrap(
            first_name=first_name,
            goal_id=goal_id,
            project_id=project_id,
        )

        wrote = stage_first_run_files(
            workspace=get_workspace(),
            owner_id=ctx.owner_id,
            friday_agent_id=friday_id,
            brief_md=brief_md,
            bootstrap_md=bootstrap_md,
        )
        if not wrote:
            return {"status": "already_submitted"}

        # Recorded LAST: a claim carrying ids always means both Paperclip
        # rows exist and are linked.
        await repo.record_onboarding_ids(
            ctx.company_id, goal_id=goal_id, project_id=project_id
        )
    except Exception:
        await repo.release_onboarding_claim(ctx.company_id)
        raise

    capture_product_event(
        distinct_id=auth.user_id,
        event="first_run_brief_submitted",
        properties={"path": body.path, "no_idea": body.input is None},
    )
    return {
        "status": "staged",
        "friday_agent_id": friday_id,
        "goal_id": goal_id,
        "project_id": project_id,
    }
```

Add the imports:

```python
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.onboarding_brief import (
    derive_goal_title,
    project_name_for,
    render_brief_md,
    render_first_run_bootstrap,
    seed_goal_and_project,
    stage_first_run_files,
)
```

**Open question this task surfaces — decide before implementing.** The spec says "the
org admin fills the brief," but removing `auth.is_org_context` wholesale lets any org
*member* submit it. The sentinel keeps that safe — only one person can seed — but
whoever gets there first supplies the provisional project name, so a non-admin member
loading `/chat` before the admin would produce "Priya's Company" for an org the admin
owns. Friday renames it either way, so the blast radius is one turn of a slightly odd
name.

Two defensible answers: gate org submits on `auth.org_role == "admin"` (matches the
spec's wording, but an org whose admin never opens the app is stuck with no
onboarding), or leave it open (any member can kick it off; the sentinel prevents
duplicates). **Recommendation: leave it open**, because the failure mode is cosmetic
and self-correcting, while the admin-gated version has a permanent stuck state. Confirm
with the requester before writing the code, and record the answer in the commit body.

Resolve the repo the same way other callers do — `grep -rn "PaperclipRepo(" apps/backend/core apps/backend/routers | grep -v test` to find the existing accessor, and use it rather than constructing one.

Update the module docstring: it currently says "Personal owners only (the funnel is trial/personal in v1 — spec §2.1)", which this task makes false.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_onboarding.py -v`
Expected: all pass, including pre-existing tests

Note: existing tests asserting `403 trial_only` / `403 personal_only` now describe removed behaviour. Delete them rather than inverting them — a test named `test_org_is_rejected` that asserts success is a trap for the next reader.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/onboarding.py apps/backend/tests/unit/routers/test_onboarding.py
git commit -m "feat(onboarding): seed goal + project, company-scoped one-shot"
```

---

### Task 5: Revised first-run runbook

Friday must name the company and project first, PATCH the goal after research, and file the three tasks into the project.

**Files:**
- Modify: `apps/backend/core/services/onboarding_brief.py` (`_FIRST_RUN_BOOTSTRAP_TEMPLATE`, `render_first_run_bootstrap`)
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py`

**Interfaces:**
- Consumes: `goal_id` / `project_id` from Task 4's call site.
- Produces: `render_first_run_bootstrap(*, first_name: str, goal_id: str, project_id: str) -> str`

**Critical:** the template is rendered with `str.format()`. Any literal `{` or `}` in added JSON bodies **must** be doubled (`{{`, `}}`) or rendering raises `KeyError`. The current template contains no braces, so this trap is new.

- [ ] **Step 1: Write the failing test**

```python
def test_bootstrap_names_before_researching():
    md = render_first_run_bootstrap(
        first_name="Max", goal_id="goal-1", project_id="proj-1"
    )
    assert md.index("Name the company") < md.index("web_search")


def test_bootstrap_carries_the_ids():
    md = render_first_run_bootstrap(
        first_name="Max", goal_id="goal-1", project_id="proj-1"
    )
    assert "goal-1" in md
    assert "proj-1" in md


def test_bootstrap_files_tasks_into_the_project():
    md = render_first_run_bootstrap(
        first_name="Max", goal_id="goal-1", project_id="proj-1"
    )
    assert "projectId" in md


def test_bootstrap_renders_with_braces_intact():
    """JSON braces in the template must survive str.format()."""
    md = render_first_run_bootstrap(
        first_name="Max", goal_id="goal-1", project_id="proj-1"
    )
    assert '"projectId"' in md
    assert "{{" not in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -v`
Expected: FAIL with `TypeError: render_first_run_bootstrap() got an unexpected keyword argument 'goal_id'`

- [ ] **Step 3: Write the implementation**

Change the signature:

```python
def render_first_run_bootstrap(*, first_name: str, goal_id: str, project_id: str) -> str:
    return _FIRST_RUN_BOOTSTRAP_TEMPLATE.format(
        first_name=first_name,
        goal_id=goal_id,
        project_id=project_id,
    )
```

In `_FIRST_RUN_BOOTSTRAP_TEMPLATE`, insert a new step 3 after the existing step 2 (read BRIEF.md), renumbering the rest:

```
3. **Name the company.** Their company and project are both currently
   named after them as a placeholder — pick a real name for the business
   from what you just read, then set it in ONE go before anything else.
   This is the first thing {first_name} sees change, so do it now, not at
   the end.

   ```
   PATCH /api/companies/$PAPERCLIP_COMPANY_ID   {{"name": "<the name>"}}
   PATCH /api/projects/{project_id}             {{"name": "<the name>"}}
   ```

   If the company PATCH 403s, keep going — the project rename is the one
   that matters and the rest of the session is unaffected.
```

After the research/docs step, insert the goal refinement:

```
6. **Write the mission onto the goal.** The company goal currently holds
   their raw words. Replace it with the mission you just wrote:

   ```
   PATCH /api/goals/{goal_id}
   {{"title": "<one-line mission>", "description": "<2-3 sentences>"}}
   ```

   Do NOT change its status or delete it — it is the company's north star
   and everything else hangs off it.
```

In the three-next-moves step, change the creation instruction so each task
carries the project:

```
   Create each with `"projectId": "{project_id}"` so it lands in their
   project and inherits the company goal. You do not need to set `goalId`
   — the board resolves it from the project.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/onboarding_brief.py \
        apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): first-run runbook names, refines goal, files into project"
```

---

### Task 6: Fix the project-creation workflow doc

`SKILL.md:305` points CEOs at `references/workflows.md` to set up projects, and that workflow never mentions linking a goal. A project with a null `goalId` orphans every task filed into it. Harmless while nothing inherits; live the moment Task 3 ships.

`references/api-reference.md:598` already shows the correct shape — this brings the shallower doc in line.

**Files:**
- Modify: `apps/infra/openclaw/skills/paperclip/references/workflows.md:7-18`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Make the edit**

Replace the numbered steps under `## Project Setup (CEO/Manager)`:

```markdown
1. Find the company goal this project serves: `GET /api/companies/{companyId}/goals`
   and take the `level: "company"` one.
2. `POST /api/companies/{companyId}/projects` with project fields **and
   `"goalIds": ["<that goal id>"]`**. Always link a goal. Tasks filed into a
   project inherit their goal from it, and a project with no goal leaves
   every task in it unattributed — the company default does NOT apply once a
   task has a project.
3. Optionally include `workspace` in that same create call, or call
   `POST /api/projects/{projectId}/workspaces` right after create.
```

- [ ] **Step 2: Verify no other doc contradicts it**

Run: `grep -rn "companies/{companyId}/projects" apps/infra/openclaw/skills/paperclip/`
Expected: `workflows.md` (now goal-linked) and `api-reference.md:592` (already shows `goalIds`). If any other file shows a project create without `goalIds`, fix it the same way.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/openclaw/skills/paperclip/references/workflows.md
git commit -m "docs(skill): link a goal when creating a project"
```

Note: this ships to the fleet only on the next OpenClaw image build + `openclaw-version.json` bump. It is not urgent — no agent creates projects during onboarding — but it should not be forgotten either.

---

## Verification

- [ ] **Full backend suite**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: no failures. If something unrelated breaks, it is probably this change — these are dependent systems.

- [ ] **Confirm the funnel still gates correctly in the frontend**

The frontend still writes `firstRun.briefSubmitted` to Clerk per user (`lib/firstRun.ts:27-32`). That is now a UI-local hint rather than the authority — the backend claim is. Verify an org's second member is not shown the funnel: their Clerk metadata is empty, so the client will render it, call `/brief`, and receive `already_submitted`. If that produces a visible flash of the funnel, follow up by having the client treat `already_submitted` as "write the marker and continue" — check `FirstRunFunnel.tsx:52-92` for where the response is handled.

- [ ] **Manual dev check**

Sign up a fresh dev account (NOT `isol8-e2e-testing@mailsac.com`), complete the funnel, and confirm in Teams that: a goal exists at company level, a project exists linked to it, Friday renames both early in her session, and the three tasks she creates sit inside the project with the goal resolved.
</content>
