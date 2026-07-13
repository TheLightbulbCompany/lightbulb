# Onboarding Aha First-Run (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New personal (free-tier) signups submit a one-line business brief while their container provisions; when it's ready, a visible auto-sent prompt kicks off Friday's first session, which researches the business and populates the task board live in the default chat+board split view.

**Architecture:** Zero new inference paths and zero serverless — the container does everything. The brief lands as OpenClaw primitives (`BRIEF.md` + a first-run `BOOTSTRAP.md` overwrite in Friday's EFS workspace, written pre-boot by a new backend endpoint) plus one Paperclip "Set up" issue assigned to Friday. UI-flow flags (`briefSubmitted`/`started`/`completed`/`skipped`) live in Clerk `user.unsafeMetadata.firstRun` — the same store `onboarded` and `getStarted` already use. The frontend inserts a funnel form into `ProvisioningStepper`'s trial cold-start path and auto-sends the kickoff prompt via `useAgentChat.sendMessage()` (which already renders the visible user bubble). Board liveness rides the existing `activity.logged → teams_event_broker → TeamsEventsProvider` SWR-invalidation chain — verified end-to-end in the spec (§4.G).

**Tech Stack:** FastAPI + pytest (backend), Next.js 16 / React 19 / SWR / vitest (frontend), Clerk unsafeMetadata, Paperclip admin client, EFS Workspace helpers.

**Spec:** `docs/superpowers/specs/2026-07-12-onboarding-aha-first-run-design.md` (v1 = §3, §4, §5 core three, §6, §7).

## Global Constraints

- **No serverless, no backend inference, no new event transport** (spec §2.3, §4.G).
- **Brief content lives ONLY in OpenClaw primitives** (EFS workspace files). No DDB onboarding table. UI-progress flags go in Clerk `unsafeMetadata` (existing convention: `onboarded`, `getStarted`).
- **Dark-ship inheritance:** every new frontend branch gates on `isTrialLocal` (`account?.subscription_status === "trial_local"`), which only exists when `FREE_TIER_ENABLED=true` (default false, `core/config.py:37`). No new feature flag needed.
- **Board-first prompt ordering** (spec §4.D): Friday claims the Set-up issue and creates task stubs before deep research.
- **Frontend vitest is RAM-heavy — run single files only**: `pnpm vitest run <file>`. Never `pnpm test` mid-task; full suites run once at the end.
- **Backend tests:** `cd apps/backend && uv run pytest <file> -v`.
- **Commits are local; never push without approval.**
- The auto-prompt and BOOTSTRAP variant are versioned together in this plan (Task 3 + Task 1) — if you change one, re-read the other for drift.

## Out of scope (do not build)

- "How It Works" landing rewrite (spec §7 — separate copy PR).
- v2/v3 artifacts (email draft, Atlas hire recs, provisioned email/landing/social).
- Mobile split view (`ChatLayout` gets the funnel + auto-send only; board stays tab-reachable).
- Routine-creation gating (spec §11.6 — deliberately left as-is).
- Team/org signups (funnel is personal-trial only).

---

### Task 1: Backend brief service (`onboarding_brief.py`)

**Files:**
- Create: `apps/backend/core/services/onboarding_brief.py`
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py`

**Interfaces:**
- Consumes: `core.containers.workspace.Workspace` (`write_file_if_missing(user_id, path, content) -> bool`, `write_file(user_id, path, content) -> None`).
- Produces (used by Task 2):
  - `render_brief_md(*, path: str, user_input: str, detail: str | None, display_name: str, email: str) -> str`
  - `render_first_run_bootstrap(*, first_name: str) -> str`
  - `setup_issue_payload(*, label: str, friday_agent_id: str) -> dict`
  - `stage_first_run_files(*, workspace, owner_id: str, friday_agent_id: str, brief_md: str, bootstrap_md: str) -> bool` — returns False when BRIEF.md already existed (one-shot replay).

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_onboarding_brief.py
"""Unit tests for the first-run brief staging service.

Mirrors tests/unit/services/test_friday_identity.py: Workspace is a
MagicMock; we assert exact workspace-relative paths and one-shot
semantics (BRIEF.md write-if-missing decides whether BOOTSTRAP.md is
overwritten and whether the caller should create the Set-up issue).
"""
from unittest.mock import MagicMock

from core.services.onboarding_brief import (
    render_brief_md,
    render_first_run_bootstrap,
    setup_issue_payload,
    stage_first_run_files,
)

FRIDAY_ID = "agent-uuid-123"
OWNER = "user_abc"


def test_render_brief_md_contains_all_fields():
    md = render_brief_md(
        path="grow",
        user_input="https://acmeconstruction.com",
        detail="residential builds in Austin",
        display_name="Max",
        email="max@acme.com",
    )
    assert "# BRIEF.md" in md
    assert "grow" in md
    assert "https://acmeconstruction.com" in md
    assert "residential builds in Austin" in md
    assert "Max" in md
    assert "max@acme.com" in md


def test_render_brief_md_omits_detail_line_when_none():
    md = render_brief_md(
        path="new",
        user_input="a social media agency for restaurants",
        detail=None,
        display_name="Ben",
        email="ben@x.com",
    )
    assert "Extra detail" not in md


def test_render_first_run_bootstrap_orders_board_first():
    md = render_first_run_bootstrap(first_name="Max")
    assert "BRIEF.md" in md
    # Board-first ordering: claiming the Set-up issue must be instructed
    # before deep research (spec §4.D).
    assert md.index("check out") < md.index("research")
    assert "Max" in md


def test_setup_issue_payload_assigns_friday_camelcase():
    payload = setup_issue_payload(label="Acme Construction", friday_agent_id=FRIDAY_ID)
    assert payload["title"] == "Set up Acme Construction"
    assert payload["assigneeAgentId"] == FRIDAY_ID
    assert "description" in payload


def test_stage_first_run_files_fresh_writes_brief_then_bootstrap():
    ws = MagicMock()
    ws.write_file_if_missing = MagicMock(return_value=True)
    ws.write_file = MagicMock()

    wrote = stage_first_run_files(
        workspace=ws,
        owner_id=OWNER,
        friday_agent_id=FRIDAY_ID,
        brief_md="brief",
        bootstrap_md="bootstrap",
    )

    assert wrote is True
    ws.write_file_if_missing.assert_called_once_with(
        OWNER, f"workspaces/{FRIDAY_ID}/BRIEF.md", "brief"
    )
    ws.write_file.assert_called_once_with(
        OWNER, f"workspaces/{FRIDAY_ID}/BOOTSTRAP.md", "bootstrap"
    )


def test_stage_first_run_files_replay_touches_nothing_else():
    ws = MagicMock()
    ws.write_file_if_missing = MagicMock(return_value=False)
    ws.write_file = MagicMock()

    wrote = stage_first_run_files(
        workspace=ws,
        owner_id=OWNER,
        friday_agent_id=FRIDAY_ID,
        brief_md="brief",
        bootstrap_md="bootstrap",
    )

    assert wrote is False
    # Replay must not clobber a BOOTSTRAP the container may have consumed.
    ws.write_file.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.onboarding_brief'`

- [ ] **Step 3: Write the implementation**

```python
# apps/backend/core/services/onboarding_brief.py
"""First-run brief staging — the onboarding aha's OpenClaw-primitive layer.

Writes two files into Friday's agent workspace on EFS BEFORE her container
boots (same pre-boot window friday_identity.py uses; OpenClaw's
writeFileIfMissing honors pre-written files):

  - ``BRIEF.md``      — the user's funnel input, verbatim + identity context.
                        Written write-if-missing: its existence IS the
                        one-shot marker for the whole flow.
  - ``BOOTSTRAP.md``  — the first-run variant, unconditionally overwritten.
                        Ordering note: friday_identity seeds the standard
                        BOOTSTRAP with write_file_if_missing, so whichever
                        of (seed, brief-submit) runs first, the first-run
                        variant wins — no race.

The spec is docs/superpowers/specs/2026-07-12-onboarding-aha-first-run-design.md
(§4.B, §4.D). The visible auto-prompt lives in the frontend
(apps/frontend/src/lib/firstRun.ts) — keep the two in sync.
"""

from __future__ import annotations

from core.containers.workspace import Workspace

_BRIEF_TEMPLATE = """\
# BRIEF.md — Onboarding brief

The human just signed up and told us about the company they want run.

- **Path:** {path}  (new = starting something new, grow = existing business)
- **Their words, verbatim:** {user_input}
{detail_line}\
- **Name:** {display_name}
- **Email:** {email}

Treat their words as the seed, not the ceiling — on the `grow` path the
input is usually their website; fetch it first. Research the person as
well as the business (their public presence tells you who you work for).
"""

_FIRST_RUN_BOOTSTRAP_TEMPLATE = """\
# BOOTSTRAP.md — Your first session is a working session

{first_name} just set up their company and is WATCHING you work: your
chat stream is on the left of their screen, the task board on the right.
A kickoff message (sent on their behalf) opens this conversation. Unlike
a normal first chat, you SHOULD use tools this session — that is the
point. Board writes are what they see, so write to Paperclip early and
often.

Load your Paperclip identity first (see AGENTS.md — the export block for
`paperclip-claimed-api-key.json`), then follow this order strictly:

1. Greet {first_name} by name in ONE line, then start working. Narrate
   briefly as you go — no walls of text between actions.
2. IMMEDIATELY find the issue titled "Set up …" on the board, check it
   out, and comment "On it — researching your market now." Do this
   before any research.
3. Quick pass: read BRIEF.md (this workspace). On the grow path, fetch
   their website once. Then create exactly 3 starter tasks on the board —
   short specific titles, one-line descriptions. Stubs are fine; you will
   enrich them in step 5.
4. Deep research with web_search: their market, competitors, positioning.
   Write findings to `research/market-research.md` in this workspace.
5. As findings land, update each starter task (better description or a
   findings comment). Re-title a stub if research invalidated it.
6. Write `MISSION.md` — what this company is, who it serves, where it's
   headed. Fold its essence into USER.md's Context section and append the
   durable facts to MEMORY.md. This becomes your permanent operating
   context.
7. Close the "Set up …" issue with a summary comment. Then close in chat:
   what you found, what you queued, and invite them to pick a task and
   hit run — "I'll do it while you watch." If they want it running while
   they're away, that's a plan upgrade — say so plainly, don't sell.

Keep the whole session under ~10 minutes of work. If the browser tab
closed mid-run (no one is replying), finish the artifacts anyway — they
are durable and {first_name} will see them on return.

## After this session

This file becomes descriptive, not prescriptive. BRIEF.md stays as the
founding record. Your real working context is IDENTITY.md, SOUL.md,
USER.md, MISSION.md, and MEMORY.md.
"""

_SETUP_ISSUE_DESCRIPTION = """\
Founding setup for {label}, run by Friday during onboarding:

- [ ] Research the business and market (see BRIEF.md in Friday's workspace)
- [ ] Create 3 starter tasks from the research
- [ ] Write the market-research doc and MISSION.md

Created automatically when the owner submitted their onboarding brief.
"""


def render_brief_md(
    *,
    path: str,
    user_input: str,
    detail: str | None,
    display_name: str,
    email: str,
) -> str:
    detail_line = f"- **Extra detail:** {detail}\n" if detail else ""
    return _BRIEF_TEMPLATE.format(
        path=path,
        user_input=user_input,
        detail_line=detail_line,
        display_name=display_name,
        email=email,
    )


def render_first_run_bootstrap(*, first_name: str) -> str:
    return _FIRST_RUN_BOOTSTRAP_TEMPLATE.format(first_name=first_name)


def setup_issue_payload(*, label: str, friday_agent_id: str) -> dict:
    """Body for PaperclipAdminClient.create_issue — raw camelCase keys
    (this call bypasses the Teams router's alias layer)."""
    return {
        "title": f"Set up {label}",
        "description": _SETUP_ISSUE_DESCRIPTION.format(label=label),
        "assigneeAgentId": friday_agent_id,
    }


def stage_first_run_files(
    *,
    workspace: Workspace,
    owner_id: str,
    friday_agent_id: str,
    brief_md: str,
    bootstrap_md: str,
) -> bool:
    """Write BRIEF.md (if missing) + first-run BOOTSTRAP.md (overwrite).

    Returns True on a fresh submit. Returns False when BRIEF.md already
    existed — a replay — in which case NOTHING else is touched (the
    container may already have consumed the bootstrap; the caller must
    also skip issue creation).
    """
    brief_rel = f"workspaces/{friday_agent_id}/BRIEF.md"
    wrote = workspace.write_file_if_missing(owner_id, brief_rel, brief_md)
    if not wrote:
        return False
    bootstrap_rel = f"workspaces/{friday_agent_id}/BOOTSTRAP.md"
    workspace.write_file(owner_id, bootstrap_rel, bootstrap_md)
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/onboarding_brief.py apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): first-run brief staging service (BRIEF.md + bootstrap variant)"
```

---

### Task 2: Backend router `POST /api/v1/onboarding/brief`

**Files:**
- Create: `apps/backend/routers/onboarding.py`
- Modify: `apps/backend/main.py` (one `include_router` line, near the `app.include_router(users.router, ...)` block at ~line 729)
- Test: `apps/backend/tests/unit/routers/test_onboarding.py`

**Interfaces:**
- Consumes: Task 1's four functions; `routers.teams.agents._ctx` / `._admin()` (TeamsContext dep + shared `PaperclipAdminClient`); `core.auth.get_current_user` → `AuthContext` (`.is_org_context`, `.user_id`, `.email`); `core.containers.get_workspace()`; `capture_product_event(*, distinct_id, event, properties=None, set_person=None)`.
- Produces (used by Task 4's frontend call): `POST /api/v1/onboarding/brief` with JSON `{path: "new"|"grow", input: str, detail?: str}` →
  - `200 {"status": "staged", "friday_agent_id": "..."}` on fresh submit
  - `200 {"status": "already_submitted"}` on replay
  - `403 {"detail": "personal_only"}` for org contexts
  - `409 {"detail": "not_ready"}` when Friday isn't in the Paperclip roster yet (client retries)

**Implementation notes for the engineer:**
- `PaperclipAdminClient.list_agents(session_cookie=..., company_id=...)` — confirm exact kwarg names against its use in `core/services/paperclip_openclaw_pairing.py:72-130` before writing the call.
- Friday = the roster entry with `role == "ceo"` (same predicate as `friday_identity.py:648-680`).
- `display_name`/`first_name`: derive from `auth.email` local-part, title-cased; fall back to `"there"`. Do NOT call Clerk here — this endpoint is on the funnel's critical path.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/routers/test_onboarding.py
"""Router tests for POST /api/v1/onboarding/brief.

Pattern mirrors tests/unit/routers/teams/test_work.py: override the
TeamsContext dependency, monkeypatch _admin() with a MagicMock carrying
AsyncMock methods, and monkeypatch get_workspace with a MagicMock.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth import AuthContext, get_current_user
import routers.onboarding as onboarding_mod
from routers.teams import agents as agents_mod

FRIDAY = {"id": "friday-uuid", "role": "ceo", "name": "Friday"}
ATLAS = {"id": "atlas-uuid", "role": "recruiter", "name": "Atlas"}


def _personal_auth():
    return AuthContext(user_id="user_abc", org_id=None, email="max@acme.com")


def _org_auth():
    return AuthContext(user_id="user_abc", org_id="org_xyz", org_role="admin", email="max@acme.com")


@pytest.fixture
def ctx():
    return SimpleNamespace(
        company_id="comp-1",
        session_cookie="cookie",
        owner_id="user_abc",
    )


@pytest.fixture
def client(ctx, monkeypatch):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[FRIDAY, ATLAS])
    admin.create_issue = AsyncMock(return_value={"id": "issue-1"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    ws = MagicMock()
    ws.write_file_if_missing = MagicMock(return_value=True)
    ws.write_file = MagicMock()
    monkeypatch.setattr(onboarding_mod, "get_workspace", lambda: ws)

    app.dependency_overrides[get_current_user] = _personal_auth
    app.dependency_overrides[onboarding_mod._ctx] = lambda: ctx
    with TestClient(app) as c:
        c.admin = admin
        c.ws = ws
        yield c
    app.dependency_overrides.clear()


def test_fresh_submit_stages_files_and_creates_issue(client):
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "grow", "input": "https://acme.com", "detail": "Austin builds"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "staged", "friday_agent_id": "friday-uuid"}

    brief_call = client.ws.write_file_if_missing.call_args
    assert brief_call.args[0] == "user_abc"
    assert brief_call.args[1] == "workspaces/friday-uuid/BRIEF.md"
    assert "https://acme.com" in brief_call.args[2]

    issue_body = client.admin.create_issue.await_args.kwargs["body"]
    assert issue_body["assigneeAgentId"] == "friday-uuid"
    assert issue_body["title"].startswith("Set up")


def test_replay_returns_already_submitted_and_skips_issue(client):
    client.ws.write_file_if_missing.return_value = False
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "already_submitted"}
    client.admin.create_issue.assert_not_awaited()
    client.ws.write_file.assert_not_called()


def test_org_context_rejected(client):
    app.dependency_overrides[get_current_user] = _org_auth
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 403


def test_no_friday_yet_returns_409(client):
    client.admin.list_agents.return_value = [ATLAS]
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 409
    client.ws.write_file_if_missing.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_onboarding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routers.onboarding'`

- [ ] **Step 3: Write the router + register it**

```python
# apps/backend/routers/onboarding.py
"""First-run onboarding endpoints.

POST /api/v1/onboarding/brief stages the funnel input as OpenClaw
primitives (BRIEF.md + first-run BOOTSTRAP.md in Friday's workspace,
pre-boot) and seeds the "Set up {label}" Paperclip issue assigned to
Friday. One-shot: BRIEF.md existence is the marker; replays are 200
no-ops. Personal owners only (the funnel is trial/personal in v1 —
spec §2.1).
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.containers import get_workspace
from core.observability.product_analytics import capture_product_event
from core.services.onboarding_brief import (
    render_brief_md,
    render_first_run_bootstrap,
    setup_issue_payload,
    stage_first_run_files,
)
from routers.teams import agents as _agents

logger = logging.getLogger(__name__)

router = APIRouter()
_ctx = _agents._ctx


class BriefBody(BaseModel):
    path: Literal["new", "grow"]
    input: str = Field(min_length=3, max_length=2000)
    detail: str | None = Field(default=None, max_length=500)


def _first_name(auth: AuthContext) -> str:
    local = (auth.email or "").split("@")[0].strip()
    return local.replace(".", " ").title() or "there"


def _label(body: BriefBody) -> str:
    """Issue-title label: strip scheme/path from grow URLs, else first words."""
    if body.path == "grow":
        host = body.input.split("//")[-1].split("/")[0].strip()
        return host or body.input[:40]
    words = body.input.split()
    return " ".join(words[:5]) + ("…" if len(words) > 5 else "")


@router.post("/brief")
async def submit_brief(
    body: BriefBody,
    auth: AuthContext = Depends(get_current_user),
    ctx=Depends(_ctx),
):
    if auth.is_org_context:
        raise HTTPException(status_code=403, detail="personal_only")

    admin = _agents._admin()
    agents = await admin.list_agents(
        session_cookie=ctx.session_cookie, company_id=ctx.company_id
    )
    friday = next((a for a in agents if a.get("role") == "ceo"), None)
    if friday is None:
        # Paperclip seed hasn't landed Friday yet — client retries.
        raise HTTPException(status_code=409, detail="not_ready")
    friday_id = friday["id"]

    first_name = _first_name(auth)
    brief_md = render_brief_md(
        path=body.path,
        user_input=body.input,
        detail=body.detail,
        display_name=first_name,
        email=auth.email or "",
    )
    bootstrap_md = render_first_run_bootstrap(first_name=first_name)

    wrote = stage_first_run_files(
        workspace=get_workspace(),
        owner_id=ctx.owner_id,
        friday_agent_id=friday_id,
        brief_md=brief_md,
        bootstrap_md=bootstrap_md,
    )
    if not wrote:
        return {"status": "already_submitted"}

    try:
        await admin.create_issue(
            company_id=ctx.company_id,
            body=setup_issue_payload(label=_label(body), friday_agent_id=friday_id),
            session_cookie=ctx.session_cookie,
        )
    except Exception:
        # Best-effort: the show still works from BRIEF.md alone (Friday's
        # bootstrap step 2 tolerates a missing Set-up issue by creating it).
        logger.exception("onboarding: Set-up issue creation failed for %s", ctx.owner_id)

    capture_product_event(
        distinct_id=auth.user_id,
        event="first_run_brief_submitted",
        properties={"path": body.path},
    )
    return {"status": "staged", "friday_agent_id": friday_id}
```

In `apps/backend/main.py`, next to the existing `include_router` block:

```python
from routers import onboarding

app.include_router(onboarding.router, prefix="/api/v1/onboarding", tags=["onboarding"])
```

Note: the bootstrap tolerance mentioned in the except-branch requires one line added to the Task 1 template's step 2 — append to that step: `If no "Set up …" issue exists, create it yourself first (title "Set up <company>"), then check it out.` Make that edit in `_FIRST_RUN_BOOTSTRAP_TEMPLATE` now and extend `test_render_first_run_bootstrap_orders_board_first` with `assert "create it yourself" in md`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_onboarding.py tests/unit/services/test_onboarding_brief.py -v`
Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/onboarding.py apps/backend/main.py apps/backend/core/services/onboarding_brief.py apps/backend/tests/
git commit -m "feat(onboarding): POST /onboarding/brief — stage BRIEF/bootstrap + seed Set-up issue"
```

---

### Task 3: Frontend first-run state lib + auto-prompt constant

**Files:**
- Create: `apps/frontend/src/lib/firstRun.ts`
- Test: `apps/frontend/src/lib/__tests__/firstRun.test.ts`

**Interfaces:**
- Consumes: Clerk `user.unsafeMetadata` (sibling-preserving merge — same pattern as `useGetStarted.ts:143`).
- Produces (used by Tasks 4–6):
  - `interface FirstRunMeta { briefSubmitted?: boolean; skipped?: boolean; started?: boolean; completed?: boolean; path?: "new" | "grow" }`
  - `readFirstRun(user): FirstRunMeta`
  - `writeFirstRun(user, patch: Partial<FirstRunMeta>): Promise<void>`
  - `FIRST_RUN_PROMPT: string`

- [ ] **Step 1: Write the failing test**

```typescript
// apps/frontend/src/lib/__tests__/firstRun.test.ts
import { describe, expect, it, vi } from "vitest";
import { FIRST_RUN_PROMPT, readFirstRun, writeFirstRun } from "@/lib/firstRun";

function fakeUser(meta: Record<string, unknown>) {
  return {
    unsafeMetadata: meta,
    update: vi.fn().mockResolvedValue(undefined),
  } as never;
}

describe("readFirstRun", () => {
  it("returns empty object when absent", () => {
    expect(readFirstRun(fakeUser({}))).toEqual({});
    expect(readFirstRun(null)).toEqual({});
  });

  it("returns the stored meta", () => {
    const u = fakeUser({ firstRun: { briefSubmitted: true, path: "grow" } });
    expect(readFirstRun(u).briefSubmitted).toBe(true);
    expect(readFirstRun(u).path).toBe("grow");
  });
});

describe("writeFirstRun", () => {
  it("merges the patch and preserves sibling metadata keys", async () => {
    const u = fakeUser({
      onboarded: true,
      getStarted: { seen: true },
      firstRun: { briefSubmitted: true },
    });
    await writeFirstRun(u, { started: true });
    const arg = (u as { update: ReturnType<typeof vi.fn> }).update.mock.calls[0][0];
    expect(arg.unsafeMetadata.onboarded).toBe(true);
    expect(arg.unsafeMetadata.getStarted).toEqual({ seen: true });
    expect(arg.unsafeMetadata.firstRun).toEqual({ briefSubmitted: true, started: true });
  });
});

describe("FIRST_RUN_PROMPT", () => {
  it("references BRIEF.md, the board, and the three starter tasks", () => {
    expect(FIRST_RUN_PROMPT).toContain("BRIEF.md");
    expect(FIRST_RUN_PROMPT).toContain("board");
    expect(FIRST_RUN_PROMPT).toContain("3 starter tasks");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/firstRun.test.ts`
Expected: FAIL — cannot resolve `@/lib/firstRun`

- [ ] **Step 3: Write the implementation**

```typescript
// apps/frontend/src/lib/firstRun.ts
/**
 * First-run ("onboarding aha") UI-flow state.
 *
 * Flags live in Clerk user.unsafeMetadata.firstRun as a SIBLING of the
 * existing `onboarded` and `getStarted` keys — every writer here (and
 * useGetStarted.ts:143) spreads unsafeMetadata, so siblings survive.
 * The brief CONTENT never lives here — it is BRIEF.md in Friday's
 * workspace (backend POST /onboarding/brief).
 *
 * FIRST_RUN_PROMPT is the visible kickoff message auto-sent to Friday
 * (spec §4.C). Its runbook counterpart is the BOOTSTRAP.md first-run
 * variant in apps/backend/core/services/onboarding_brief.py — keep the
 * two in sync (both name BRIEF.md, the Set-up issue, 3 starter tasks,
 * the research doc, MISSION.md).
 */
import type { UserResource } from "@clerk/types";

export interface FirstRunMeta {
  briefSubmitted?: boolean;
  skipped?: boolean;
  started?: boolean;
  completed?: boolean;
  path?: "new" | "grow";
}

export function readFirstRun(user: UserResource | null | undefined): FirstRunMeta {
  const meta = user?.unsafeMetadata?.firstRun;
  return (meta && typeof meta === "object" ? meta : {}) as FirstRunMeta;
}

export async function writeFirstRun(
  user: UserResource,
  patch: Partial<FirstRunMeta>,
): Promise<void> {
  await user.update({
    unsafeMetadata: {
      ...user.unsafeMetadata,
      firstRun: { ...readFirstRun(user), ...patch },
    },
  });
}

export const FIRST_RUN_PROMPT = [
  "Friday — I just set up my company. Read BRIEF.md in your workspace and run my onboarding:",
  "claim the \"Set up\" task on the board, create 3 starter tasks from a quick first pass,",
  "then research my market properly, enrich the tasks with what you find, write the research",
  "doc and MISSION.md, and close out with a summary. Narrate as you go — I'm watching the board.",
].join(" ");
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/firstRun.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/lib/firstRun.ts apps/frontend/src/lib/__tests__/firstRun.test.ts
git commit -m "feat(onboarding): firstRun metadata lib + visible kickoff prompt"
```

---

### Task 4: Funnel form in ProvisioningStepper

**Files:**
- Create: `apps/frontend/src/components/onboarding/FirstRunFunnel.tsx`
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx` (new branch immediately before the fall-through cold-start return at ~line 1121)
- Modify: `apps/frontend/src/lib/analytics.ts` (add to the `AnalyticsEvent` union at ~lines 124–132: `"first_run_brief_submitted" | "first_run_brief_skipped" | "first_run_kickoff_sent" | "first_run_show_completed"`)
- Test: `apps/frontend/src/components/onboarding/__tests__/FirstRunFunnel.test.tsx`

**Interfaces:**
- Consumes: `useApi().post(endpoint, body)` (`@/lib/api`); `readFirstRun`/`writeFirstRun` (Task 3); `useUser()` (Clerk); `capture` (`@/lib/analytics`); stepper locals `isTrialLocal` (ProvisioningStepper.tsx:227).
- Produces: `<FirstRunFunnel />` — self-contained; on submit/skip it writes `firstRun` metadata, which flips the stepper branch off and falls through to the existing cold-start card ("spinning up" state — no new theater, per spec §3).

**Behavior spec for the component:**
1. Screen 1 — two buttons: **"Start something new"** / **"Grow my business"** (full-screen, modeled on the provider step's layout wrapper, ProvisioningStepper.tsx:787-813).
2. Screen 2 — single input (textarea for `new` with placeholder "e.g. A social media agency for small restaurants"; text input for `grow` with placeholder "yourcompany.com") + one optional detail field on the `new` path ("Who's it for? (optional)") + Continue button + a quiet "Skip for now" link.
3. Submit: `api.post("/onboarding/brief", { path, input, detail })`. On success or `"already_submitted"`: `writeFirstRun(user, { briefSubmitted: true, path })` + `capture("first_run_brief_submitted", { path })`. On 409 (`not_ready`): retry up to 5 times at 3s intervals with a "Setting things up…" note; after that show an inline error with a Retry button (do not lose the typed input). On skip: `writeFirstRun(user, { skipped: true })` + `capture("first_run_brief_skipped")`.
4. The stepper branch (in `ProvisioningStepper`, before line 1121):

```tsx
// First-run funnel: trial signups describe their business while the
// container provisions (spec §3). Flag writes flip this branch off and
// fall through to the cold-start card below.
const firstRun = readFirstRun(user);
if (
  isTrialLocal &&
  !firstRun.briefSubmitted &&
  !firstRun.skipped &&
  (phase === "container" || phase === "gateway" || phase === "channels-sidecars" || phase === "agent")
) {
  return <FirstRunFunnel />;
}
```

(`user` via `useUser()` — the stepper already imports Clerk hooks; check the top of the file and reuse the existing import.)

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/onboarding/__tests__/FirstRunFunnel.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const update = vi.fn().mockResolvedValue(undefined);
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { unsafeMetadata: {}, update },
    isLoaded: true,
  }),
}));

import { FirstRunFunnel } from "@/components/onboarding/FirstRunFunnel";

beforeEach(() => {
  post.mockReset();
  update.mockReset().mockResolvedValue(undefined);
});

describe("FirstRunFunnel", () => {
  it("walks grow path: choose -> enter url -> submit -> writes metadata", async () => {
    post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
    render(<FirstRunFunnel />);

    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "acme.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/onboarding/brief", {
        path: "grow",
        input: "acme.com",
        detail: undefined,
      });
    });
    await waitFor(() => {
      const meta = update.mock.calls[0][0].unsafeMetadata.firstRun;
      expect(meta.briefSubmitted).toBe(true);
      expect(meta.path).toBe("grow");
    });
  });

  it("skip writes skipped flag without posting", async () => {
    render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
    fireEvent.click(screen.getByRole("button", { name: /skip for now/i }));
    await waitFor(() => {
      expect(update.mock.calls[0][0].unsafeMetadata.firstRun.skipped).toBe(true);
    });
    expect(post).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/onboarding/__tests__/FirstRunFunnel.test.tsx`
Expected: FAIL — cannot resolve `@/components/onboarding/FirstRunFunnel`

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/onboarding/FirstRunFunnel.tsx
"use client";

/**
 * Two-path onboarding funnel shown while the trial container provisions
 * (spec §3). Submitting stages BRIEF.md server-side; the firstRun
 * metadata write flips ProvisioningStepper's branch off, falling through
 * to the cold-start card. Skippable — skipping falls back to today's flow.
 */
import * as React from "react";
import { useUser } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import { writeFirstRun } from "@/lib/firstRun";

type Path = "new" | "grow";
const RETRY_MS = 3000;
const MAX_RETRIES = 5;

export function FirstRunFunnel() {
  const { user } = useUser();
  const api = useApi();
  const [path, setPath] = React.useState<Path | null>(null);
  const [input, setInput] = React.useState("");
  const [detail, setDetail] = React.useState("");
  const [state, setState] = React.useState<"idle" | "submitting" | "waiting" | "error">("idle");

  const submit = React.useCallback(async () => {
    if (!user || input.trim().length < 3) return;
    setState("submitting");
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        await api.post("/onboarding/brief", {
          path,
          input: input.trim(),
          detail: path === "new" && detail.trim() ? detail.trim() : undefined,
        });
        await writeFirstRun(user, { briefSubmitted: true, path: path ?? undefined });
        capture("first_run_brief_submitted", { path });
        return;
      } catch (err) {
        const status = (err as { status?: number }).status;
        if (status === 409 && attempt < MAX_RETRIES) {
          setState("waiting");
          await new Promise((r) => setTimeout(r, RETRY_MS));
          continue;
        }
        setState("error");
        return;
      }
    }
  }, [api, user, path, input, detail]);

  const skip = React.useCallback(async () => {
    if (!user) return;
    await writeFirstRun(user, { skipped: true });
    capture("first_run_brief_skipped");
  }, [user]);

  return (
    <div className="flex-1 flex items-center justify-center p-6 bg-[#faf7f2]">
      <div className="w-full max-w-lg space-y-6">
        {path === null ? (
          <>
            <h1 className="text-3xl font-semibold text-center">Let&apos;s get started.</h1>
            <button
              className="w-full rounded-lg bg-black text-white py-4 text-sm tracking-wide"
              onClick={() => setPath("new")}
            >
              Start something new
            </button>
            <button
              className="w-full rounded-lg bg-black text-white py-4 text-sm tracking-wide"
              onClick={() => setPath("grow")}
            >
              Grow my business
            </button>
          </>
        ) : (
          <>
            <button className="text-xs text-neutral-500" onClick={() => setPath(null)}>
              ← Back
            </button>
            <h1 className="text-3xl font-semibold">
              {path === "new" ? "What's your idea?" : "What's your company's website?"}
            </h1>
            {path === "new" ? (
              <>
                <textarea
                  className="w-full border rounded-lg p-3 min-h-28"
                  placeholder="e.g. A social media agency for small restaurants"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                />
                <input
                  className="w-full border rounded-lg p-3"
                  placeholder="Who's it for? (optional)"
                  value={detail}
                  onChange={(e) => setDetail(e.target.value)}
                />
              </>
            ) : (
              <input
                className="w-full border rounded-lg p-3"
                placeholder="yourcompany.com"
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
            )}
            <button
              className="w-full rounded-lg bg-black text-white py-4 text-sm tracking-wide disabled:opacity-50"
              disabled={state === "submitting" || state === "waiting" || input.trim().length < 3}
              onClick={submit}
            >
              {state === "waiting" ? "Setting things up…" : "Continue →"}
            </button>
            {state === "error" && (
              <p className="text-sm text-red-600">
                Couldn&apos;t save that just yet.{" "}
                <button className="underline" onClick={submit}>
                  Retry
                </button>
              </p>
            )}
            <button className="block mx-auto text-xs text-neutral-400" onClick={skip}>
              Skip for now
            </button>
          </>
        )}
      </div>
    </div>
  );
}
```

Then add the stepper branch (code in the Behavior spec above) to `ProvisioningStepper.tsx` before the fall-through return at ~line 1121, and add the four event names to the `AnalyticsEvent` union in `analytics.ts`.

- [ ] **Step 4: Run tests**

Run: `cd apps/frontend && pnpm vitest run src/components/onboarding/__tests__/FirstRunFunnel.test.tsx`
Expected: PASS
Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/ProvisioningStepper.test.tsx`
Expected: PASS (existing suite unaffected — the new branch requires `isTrialLocal`)

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/onboarding/ apps/frontend/src/components/chat/ProvisioningStepper.tsx apps/frontend/src/lib/analytics.ts
git commit -m "feat(onboarding): first-run funnel form during trial provisioning"
```

---

### Task 5: Auto-send kickoff in AgentChatWindow

**Files:**
- Modify: `apps/frontend/src/components/chat/AgentChatWindow.tsx` (effect near where `useAgentChat` is held; `sendMessage` and `onChatCompleted` already exist there — see `AgentChatWindow.tsx:244` for `sessionName`)
- Test: `apps/frontend/src/components/chat/__tests__/AgentChatWindowFirstRun.test.tsx` (new file — do NOT extend an existing heavy suite)

**Interfaces:**
- Consumes: `useAgentChat(agentId, sessionName, onChatCompleted)` → `sendMessage(message: string): Promise<void>` (useAgentChat.ts:1830); `useAgents().defaultId`; `readFirstRun`/`writeFirstRun`/`FIRST_RUN_PROMPT` (Task 3); `capture`.
- Produces: on mount with (agentId === defaultId && firstRun.briefSubmitted && !firstRun.started), sends `FIRST_RUN_PROMPT` once, writes `{started: true}`, captures `first_run_kickoff_sent`. When a chat run completes while (started && !completed), writes `{completed: true}` and captures `first_run_show_completed`.

**Implementation notes:**
- AgentChatWindow only renders when the stepper reached `phase === "ready"` (it renders as `children` of the stepper), so mount ≈ container ready — no extra readiness condition needed beyond gateway `isConnected` if already available in the component; `sendMessage` queues safely if a run is in flight.
- Guard with a `useRef` so React strict-mode double-effects can't double-send; `writeFirstRun({started:true})` is the durable cross-reload guard.
- Session: the default General session (`activeSessionId === null`) — nothing to set.
- The exact effect:

```tsx
const firstRunFired = React.useRef(false);
React.useEffect(() => {
  if (firstRunFired.current || !user) return;
  const fr = readFirstRun(user);
  if (!fr.briefSubmitted || fr.started) return;
  if (!agentId || agentId !== defaultId) return;
  firstRunFired.current = true;
  void (async () => {
    await writeFirstRun(user, { started: true });
    await sendMessage(FIRST_RUN_PROMPT);
    capture("first_run_kickoff_sent");
  })();
}, [user, agentId, defaultId, sendMessage]);
```

And in the existing `onChatCompleted` callback path (the same one that powers `trial_first_agent_response` at AgentChatWindow.tsx:268):

```tsx
const fr = readFirstRun(user);
if (fr.started && !fr.completed) {
  void writeFirstRun(user, { completed: true });
  capture("first_run_show_completed");
}
```

- [ ] **Step 1: Write the failing test** — a focused hook-level test of the effect logic. Extract the effect into `apps/frontend/src/hooks/useFirstRunKickoff.ts` so it is testable without rendering the full AgentChatWindow:

```typescript
// apps/frontend/src/hooks/useFirstRunKickoff.ts
"use client";
import * as React from "react";
import type { UserResource } from "@clerk/types";
import { capture } from "@/lib/analytics";
import { FIRST_RUN_PROMPT, readFirstRun, writeFirstRun } from "@/lib/firstRun";

/** Fire the visible first-run kickoff prompt exactly once (spec §4.C). */
export function useFirstRunKickoff(opts: {
  user: UserResource | null | undefined;
  agentId: string | null;
  defaultId: string | null | undefined;
  sendMessage: (message: string) => Promise<void>;
}) {
  const { user, agentId, defaultId, sendMessage } = opts;
  const fired = React.useRef(false);
  React.useEffect(() => {
    if (fired.current || !user) return;
    const fr = readFirstRun(user);
    if (!fr.briefSubmitted || fr.started) return;
    if (!agentId || !defaultId || agentId !== defaultId) return;
    fired.current = true;
    void (async () => {
      await writeFirstRun(user, { started: true });
      await sendMessage(FIRST_RUN_PROMPT);
      capture("first_run_kickoff_sent");
    })();
  }, [user, agentId, defaultId, sendMessage]);
}
```

```tsx
// apps/frontend/src/hooks/__tests__/useFirstRunKickoff.test.tsx
import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { waitFor } from "@testing-library/react";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { useFirstRunKickoff } from "@/hooks/useFirstRunKickoff";
import { FIRST_RUN_PROMPT } from "@/lib/firstRun";

function fakeUser(firstRun: Record<string, unknown>) {
  return {
    unsafeMetadata: { firstRun },
    update: vi.fn().mockResolvedValue(undefined),
  } as never;
}

describe("useFirstRunKickoff", () => {
  it("sends the prompt once when brief submitted and not started", async () => {
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true });
    const { rerender } = renderHook(() =>
      useFirstRunKickoff({ user, agentId: "a1", defaultId: "a1", sendMessage }),
    );
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    rerender();
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("does nothing when already started", () => {
    const sendMessage = vi.fn();
    const user = fakeUser({ briefSubmitted: true, started: true });
    renderHook(() =>
      useFirstRunKickoff({ user, agentId: "a1", defaultId: "a1", sendMessage }),
    );
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("does nothing on a non-default agent window", () => {
    const sendMessage = vi.fn();
    const user = fakeUser({ briefSubmitted: true });
    renderHook(() =>
      useFirstRunKickoff({ user, agentId: "a2", defaultId: "a1", sendMessage }),
    );
    expect(sendMessage).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/hooks/__tests__/useFirstRunKickoff.test.tsx`
Expected: FAIL — cannot resolve `@/hooks/useFirstRunKickoff`

- [ ] **Step 3: Implement** — create the hook file exactly as above; wire it inside `AgentChatWindow` where `useAgentChat` is held (`useFirstRunKickoff({ user, agentId, defaultId, sendMessage })`, with `defaultId` from `useAgents()` — already imported in the shell path; add the import if AgentChatWindow lacks it). Add the completion write to the existing `onChatCompleted` path (code in Implementation notes).

- [ ] **Step 4: Run tests**

Run: `cd apps/frontend && pnpm vitest run src/hooks/__tests__/useFirstRunKickoff.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/hooks/useFirstRunKickoff.ts apps/frontend/src/hooks/__tests__/useFirstRunKickoff.test.tsx apps/frontend/src/components/chat/AgentChatWindow.tsx
git commit -m "feat(onboarding): auto-send visible first-run kickoff prompt"
```

---

### Task 6: Get-started suppression + board-pulse telemetry

**Files:**
- Modify: `apps/frontend/src/components/getstarted/GetStarted.tsx` (null-return at line 13)
- Modify: `apps/frontend/src/components/teams/TeamsEventsProvider.tsx` (onEvent handler, lines 101–115)
- Modify: `apps/frontend/src/lib/analytics.ts` (add `"first_run_board_pulse"` to the union)
- Test: `apps/frontend/src/components/getstarted/__tests__/GetStartedFirstRun.test.tsx`

**Interfaces:**
- Consumes: `readFirstRun` (Task 3), `useUser()`, `capture`.
- Produces: (a) the get-started widget never renders for a user who submitted a brief (spec §7 — "what Friday already did" supersedes "what you must do"); (b) during an active show (`started && !completed`), each `teams.activity.logged` event captures `first_run_board_pulse` — the prod telemetry that pulses reached this client (spec §4.G gate 1's measurable proxy; true e2e latency needs a BFF-side emit timestamp, which `activity.logged` payloads don't carry — noted as a follow-up if the pulse counts look wrong).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/getstarted/__tests__/GetStartedFirstRun.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { unsafeMetadata: { firstRun: { briefSubmitted: true } }, update: vi.fn() },
    isLoaded: true,
  }),
}));

import { GetStarted } from "@/components/getstarted/GetStarted";

describe("GetStarted with an active first-run", () => {
  it("renders nothing when the user submitted a first-run brief", () => {
    const derived = { chat: false, task: false, hire: false, channel: false };
    render(<GetStarted derived={derived as never} actions={{} as never} />);
    expect(screen.queryByText(/get started/i)).toBeNull();
  });
});
```

(Adjust the `derived`/`actions` prop shapes to the real `GetStarted` prop types when implementing — read `GetStarted.tsx:10` first; the assertion is the null render.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedFirstRun.test.tsx`
Expected: FAIL — the launcher renders

- [ ] **Step 3: Implement**

In `GetStarted.tsx`, extend the existing null-return (line 13):

```tsx
const { user } = useUser();
const firstRun = readFirstRun(user);
// First-run users saw Friday do the checklist's steps herself (spec §7).
if (firstRun.briefSubmitted) return null;
if (gs.dismissed || gs.completedFired || complete) return null;
```

In `TeamsEventsProvider.tsx`, inside the `onEvent` handler after the `teams.` prefix check:

```tsx
if (event === "teams.activity.logged") {
  const fr = readFirstRun(user);
  if (fr.started && !fr.completed) capture("first_run_board_pulse");
}
```

(`useUser()` import at the top of the provider; `capture` from `@/lib/analytics`.)

- [ ] **Step 4: Run tests**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedFirstRun.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/getstarted/ apps/frontend/src/components/teams/TeamsEventsProvider.tsx apps/frontend/src/lib/analytics.ts
git commit -m "feat(onboarding): retire get-started for first-run users + board-pulse telemetry"
```

---

### Task 7: Full-suite verification + dev acceptance run

**Files:** none created — verification only.

- [ ] **Step 1: Backend full test suite**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: all PASS (if anything unrelated fails, YOUR change is the likely culprit — investigate before blaming the test).

- [ ] **Step 2: Frontend typecheck + lint** (tsc typechecks test files too)

Run: `cd apps/frontend && pnpm run lint && npx tsc --noEmit`
Expected: clean

- [ ] **Step 3: Frontend full unit suite** (once, at the end — RAM-heavy)

Run: `cd apps/frontend && pnpm test`
Expected: all PASS

- [ ] **Step 4: Manual dev acceptance (spec §4.G gate 5 — required before enabling for real signups)**

With `FREE_TIER_ENABLED=true` on dev and a fresh test account (NOT `isol8-e2e-testing@mailsac.com`):
1. Sign up → funnel appears while container provisions → submit a grow-path brief with a real small-business URL.
2. Cold-start card shows until ready; then the kickoff prompt visibly appears in chat and Friday starts.
3. Watch both panes ≥10 minutes: the Set-up issue is claimed within ~30s of session start; 3 task stubs appear within ~2 minutes; each board change renders WITHOUT any refresh (spec gates 1–3); research doc + MISSION.md exist in the Files panel afterward.
4. Click run on one starter task → run-detail view (`teams-run-detail` panel) streams the run (the coda).
5. Reload mid-show → show resumes (no duplicate kickoff — `started` flag holds).
6. Record signup→handshake elapsed time from the stepper clock (baseline for the spec §10 time-to-show metric).

- [ ] **Step 5: Commit any fixups; stop.**

Do NOT push — pushing and PR creation require explicit approval.

---

## Self-review notes (already applied)

- **Spec coverage:** §3 timeline → Tasks 4+5; §4.B → Tasks 1+2; §4.C → Tasks 3+5; §4.D board-first runbook → Task 1's BOOTSTRAP template + Task 3's prompt; §4.G gates → Task 6 telemetry + Task 7 manual gates (gate 4's hermetic E2E is deferred — the SWR-invalidation chain is pinned by TeamsEventsProvider's existing behavior and the manual gate; noted as a fast-follow); §4.H → no B-runs triggered anywhere in this plan; §6 → no billing changes (verified none needed); §7 → Task 6 (get-started) — stepper card intentionally kept as the waiting state.
- **Known intentional deviations:** spec §3's "Spinning up your company's container…" copy — v1 reuses the existing cold-start card copy verbatim; copy tweak is a follow-up. Spec gate 1's "latency metric" ships as a pulse-count proxy (payload lacks an emit timestamp).
- **Type consistency:** `FirstRunMeta` fields (`briefSubmitted/skipped/started/completed/path`) used identically in Tasks 3–6; `stage_first_run_files` signature identical in Tasks 1–2; `FIRST_RUN_PROMPT` produced in Task 3, consumed in Task 5.
