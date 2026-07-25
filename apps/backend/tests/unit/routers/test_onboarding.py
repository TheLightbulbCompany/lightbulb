"""Router tests for POST /api/v1/onboarding/brief.

Pattern mirrors tests/unit/routers/teams/test_work.py: override the
TeamsContext dependency, monkeypatch _admin() with a MagicMock carrying
AsyncMock methods, and monkeypatch get_workspace with a MagicMock.

Onboarding is one-shot per COMPANY, fenced by the paperclip-companies OWNER
row's ``onboarding_generation`` (spec PR-4 / ONBOARD-TABLE) -- ``client.claim``
/ ``client.revert`` / ``client.record`` stand in for
``claim_funnel_submission`` / ``revert_funnel_submission`` /
``record_onboarding_ids``. This replaces the old ``__onboarded__`` sentinel's
``claim_onboarding`` / ``release_onboarding_claim`` / ``record_onboarding_ids``
vocabulary entirely.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth import AuthContext, get_current_user
from core.repositories.paperclip_repo import FunnelClaimResult
from core.services.paperclip_admin_client import PaperclipApiError
import routers.onboarding as onboarding_mod
from routers.teams import agents as agents_mod

FRIDAY = {"id": "friday-uuid", "role": "ceo", "name": "Friday"}
ATLAS = {"id": "atlas-uuid", "role": "recruiter", "name": "Atlas"}

# The owner row resolve_owner_row(ctx.owner_id) returns. ``user_id`` is
# deliberately DIFFERENT from ctx.owner_id ("user_abc") to catch any code
# path that accidentally keys the claim/record/revert calls on ctx.owner_id
# instead of owner_row.user_id -- for an org, those two are genuinely
# different values (see PaperclipRepo.resolve_owner_row's docstring).
# bootstrap_state/bootstrap_attempts are read by submit_brief when it builds
# the onboarding dict passed to maybe_dispatch_bootstrap (see that call site).
OWNER_ROW = SimpleNamespace(
    user_id="owner-row-user",
    onboarding_generation="01GEN",
    bootstrap_state="not_started",
    bootstrap_attempts=0,
)


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
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(return_value={"id": "proj-1"})
    admin.delete_goal = AsyncMock()
    admin.delete_project = AsyncMock()
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    ws = MagicMock()
    ws.write_file = MagicMock()
    monkeypatch.setattr(onboarding_mod, "get_workspace", lambda: ws)

    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=OWNER_ROW)
    repo.claim_funnel_submission = AsyncMock(return_value=FunnelClaimResult.WON)
    repo.revert_funnel_submission = AsyncMock()
    repo.record_onboarding_ids = AsyncMock()
    monkeypatch.setattr(onboarding_mod, "PaperclipRepo", lambda **kwargs: repo)

    # The post-submit bootstrap-dispatch kickoff is fire-and-forget
    # (asyncio.create_task) -- close the coroutine instead of letting it run
    # against real DDB/gateway state in these router-level tests. Individual
    # tests that care about the dispatch call assert directly against
    # dispatch_bootstrap below.
    dispatch_bootstrap = AsyncMock()
    monkeypatch.setattr(onboarding_mod, "maybe_dispatch_bootstrap", dispatch_bootstrap)

    app.dependency_overrides[get_current_user] = _personal_auth
    app.dependency_overrides[onboarding_mod._ctx] = lambda: ctx
    with TestClient(app) as c:
        c.admin = admin
        c.ws = ws
        c.repo = repo
        c.resolve_owner_row = repo.resolve_owner_row
        c.claim = repo.claim_funnel_submission
        c.revert = repo.revert_funnel_submission
        c.record = repo.record_onboarding_ids
        c.dispatch_bootstrap = dispatch_bootstrap
        yield c
    app.dependency_overrides.clear()


def test_fresh_submit_stages_files_without_seeding_a_board_issue(client):
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "grow", "input": "https://acme.com", "detail": "Austin builds"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "staged"
    assert body["friday_agent_id"] == "friday-uuid"
    assert body["goal_id"] == "goal-1"
    assert body["project_id"] == "proj-1"

    brief_call = client.ws.write_file.call_args_list[0]
    assert brief_call.args[0] == "user_abc"  # ctx.owner_id — the workspace path
    assert brief_call.args[1] == "workspace-friday-uuid/BRIEF.md"
    assert "https://acme.com" in brief_call.args[2]

    # The board is no longer seeded on submit — Friday fills it at the END of
    # her research session with the three next-move tasks she proposes.
    client.admin.create_issue.assert_not_called()


def test_success_does_not_touch_the_containers_table(client):
    """S2 (containers.onboarded_at) is deleted entirely -- a successful
    submit only advances funnel_state on the paperclip-companies owner row.
    There is no container_repo import left in the router to monkeypatch, so
    this asserts the module attribute is simply gone."""
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "An agency for restaurants"},
    )
    assert resp.status_code == 200
    assert not hasattr(onboarding_mod, "container_repo")


def test_no_friday_yet_returns_409(client):
    client.admin.list_agents.return_value = [ATLAS]
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_ready"
    client.ws.write_file.assert_not_called()


def test_paperclip_error_returns_retryable_409(client):
    client.admin.list_agents.side_effect = PaperclipApiError("paperclip down", status_code=503, body=None)
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_ready"
    client.ws.write_file.assert_not_called()


def test_submit_captures_analytics_event(client, monkeypatch):
    capture = MagicMock()
    monkeypatch.setattr(onboarding_mod, "capture_product_event", capture)
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "grow", "input": "https://acme.com"},
    )
    assert resp.status_code == 200
    capture.assert_called_once()
    kwargs = capture.call_args.kwargs
    assert kwargs["event"] == "first_run_brief_submitted"
    assert kwargs["properties"] == {"path": "grow", "no_idea": False}


# --- "I don't know yet — you pick": submit with no input ---------------------


def test_new_path_without_input_stages_a_pick_one_brief(client):
    """The absent idea IS the signal — Friday is told to choose one."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "staged"

    brief = client.ws.write_file.call_args_list[0].args[2]
    assert "don't have an\n  idea yet" in brief
    assert "job zero" in brief
    # Her only lead is who they are — the brief must still carry it.
    assert "max@acme.com" in brief


def test_new_path_with_blank_input_is_treated_as_no_idea(client):
    """A client posting "" must not produce a brief quoting an empty string."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "   "})
    assert resp.status_code == 200

    brief = client.ws.write_file.call_args_list[0].args[2]
    assert "job zero" in brief


def test_no_idea_submit_is_flagged_in_analytics(client, monkeypatch):
    capture = MagicMock()
    monkeypatch.setattr(onboarding_mod, "capture_product_event", capture)
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new"})
    assert resp.status_code == 200
    assert capture.call_args.kwargs["properties"] == {"path": "new", "no_idea": True}


def test_grow_path_still_requires_input(client):
    """Only `new` may omit the idea — a grow brief with no website is useless."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "grow"})
    assert resp.status_code == 422
    client.ws.write_file.assert_not_called()


def test_too_short_input_still_rejected(client):
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "ab"})
    assert resp.status_code == 422
    client.ws.write_file.assert_not_called()


# --- Owner-row resolution + generation-fenced claim ---------------------------


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


def test_claim_and_record_use_the_owner_rows_user_id_and_generation(client):
    """The route must fence claim/record on owner_row.user_id +
    owner_row.onboarding_generation -- NOT ctx.owner_id (which, for an org,
    is a DIFFERENT value: the Clerk org_id, not the row's PK) and NOT
    ctx.company_id (the old sentinel's key, no longer relevant)."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    # Strong read on the claim path (read-then-write the same row); the
    # per-poll get_onboarding_state path stays eventually consistent.
    client.resolve_owner_row.assert_awaited_once_with("user_abc", consistent_read=True)  # ctx.owner_id
    # DDB key is owner_row.user_id; submitted_by is auth.user_id (the ACTUAL
    # submitting member); the realtime nudge rides owner_id=ctx.owner_id (the
    # org broadcast key). For an org all three are genuinely distinct values;
    # in this personal-owner fixture submitted_by == owner_id == "user_abc".
    client.claim.assert_awaited_once_with(
        "owner-row-user",
        onboarding_generation="01GEN",
        submitted_by="user_abc",
        owner_id="user_abc",
        # The user's brief is stamped on the SAME claim write (new path, no
        # audience -> the verbatim idea).
        brief_text="An agency",
    )
    client.record.assert_awaited_once_with(
        "owner-row-user", onboarding_generation="01GEN", goal_id="goal-1", project_id="proj-1", owner_id="user_abc"
    )


def test_no_owner_row_yet_returns_409(client):
    """No paperclip-companies row for this owner yet -- nothing to fence a
    claim against. The client already retries on 409."""
    client.resolve_owner_row.return_value = None

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_ready"
    client.claim.assert_not_awaited()


def test_owner_row_without_onboarding_generation_returns_409(client):
    """Defensive: a row that predates G2 (should not happen post-ONBOARD-TABLE
    for any row provision_org just wrote) is treated the same as no row."""
    client.resolve_owner_row.return_value = SimpleNamespace(user_id="owner-row-user", onboarding_generation=None)

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.claim.assert_not_awaited()


def test_second_submit_for_same_company_is_a_noop(client):
    """ALREADY_SUBMITTED means a peer's claim already won (or the funnel
    already completed) on the SAME generation this caller just read."""
    client.claim.return_value = FunnelClaimResult.ALREADY_SUBMITTED

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.json() == {"status": "already_submitted"}
    client.admin.create_goal.assert_not_awaited()
    client.ws.write_file.assert_not_called()


def test_stale_generation_claim_is_a_retryable_409_not_already_submitted(client):
    """A superseded onboarding_generation (the company was reprovisioned
    between this caller's read and its write) must NOT be reported as
    already_submitted -- the caller was holding a stale view of the company,
    not colliding with a duplicate submit. It must be a retryable 409
    instead, matching the other not_ready cases the client already retries
    on."""
    client.claim.return_value = FunnelClaimResult.STALE_GENERATION

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_ready"
    client.admin.create_goal.assert_not_awaited()
    client.ws.write_file.assert_not_called()
    # Not this caller's claim to revert -- it never won one.
    client.revert.assert_not_awaited()


def test_paperclip_not_ready_reverts_the_claim(client):
    client.admin.list_agents = AsyncMock(side_effect=PaperclipApiError("boom", status_code=503, body=None))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.revert.assert_awaited_once_with("owner-row-user", onboarding_generation="01GEN", owner_id="user_abc")


def test_seed_failure_reverts_the_claim(client):
    client.admin.create_goal = AsyncMock(side_effect=RuntimeError("upstream down"))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.revert.assert_awaited_once_with("owner-row-user", onboarding_generation="01GEN", owner_id="user_abc")
    client.ws.write_file.assert_not_called()


# --- Post-seed failure rolls back both seeded rows (FIX 1) -------------------


def test_post_seed_failure_rolls_back_goal_and_project_then_reverts_claim(client):
    """Once seed_goal_and_project has returned ids, a later failure (e.g. a
    DynamoDB throttle on record_onboarding_ids, which is NOT in the caught
    (PaperclipApiError, httpx.HTTPError, RuntimeError) set and so escapes as
    a raw 500) must delete both seeded Paperclip rows before reverting the
    claim. Otherwise a client retry wins a fresh claim and seeds a SECOND
    goal/project pair for the same company."""
    client.record.side_effect = Exception("dynamodb throttled")

    with pytest.raises(Exception, match="dynamodb throttled"):
        client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    client.admin.delete_project.assert_awaited_once_with(session_cookie="cookie", project_id="proj-1")
    client.admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")
    client.revert.assert_awaited_once_with("owner-row-user", onboarding_generation="01GEN", owner_id="user_abc")


def test_post_seed_cleanup_is_failure_tolerant_and_preserves_the_original_error(client):
    """A failure deleting the project must not skip the goal delete or the
    claim revert, and the cleanup failure itself must not mask the
    ORIGINAL error the caller sees."""
    client.record.side_effect = Exception("dynamodb throttled")
    client.admin.delete_project.side_effect = Exception("project delete also failed")

    with pytest.raises(Exception, match="dynamodb throttled"):
        client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    client.admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")
    client.revert.assert_awaited_once_with("owner-row-user", onboarding_generation="01GEN", owner_id="user_abc")


# --- Server-owned bootstrap dispatch kickoff (spec PR-6, BOOTSTRAP) ----------


def test_successful_submit_kicks_off_bootstrap_dispatch(client):
    """A successful /brief submit dispatches the bootstrap kickoff in the
    background, in case the runtime is ALREADY up -- see
    core.services.bootstrap_dispatcher's module docstring. Fire-and-forget:
    the response doesn't wait on it, but the coroutine is scheduled with
    ctx.owner_id AND the onboarding dict built from the post-claim state --
    so the dispatcher never re-reads the row (see the call site's comment)."""
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "An agency"},
    )
    assert resp.status_code == 200
    client.dispatch_bootstrap.assert_awaited_once_with(
        "user_abc",  # ctx.owner_id
        onboarding={
            "onboarding_generation": "01GEN",
            "funnel_state": "submitted",
            "bootstrap_state": "not_started",
            "bootstrap_attempts": 0,
            "user_id": "owner-row-user",
            "onboarding_submitted_by": "user_abc",  # auth.user_id -- the actual submitter
            "onboarding_brief_text": "An agency",  # the user's brief, threaded inline
        },
    )


def test_already_submitted_does_not_kick_off_bootstrap_dispatch(client):
    """No fresh claim, no new dispatch to kick off -- the container/status
    poll path is what handles a bootstrap for an already-submitted funnel."""
    client.claim.return_value = FunnelClaimResult.ALREADY_SUBMITTED

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.json() == {"status": "already_submitted"}
    client.dispatch_bootstrap.assert_not_awaited()


def test_seed_only_failure_does_not_attempt_rollback(client):
    """When seed_goal_and_project itself fails (goal_id/project_id never
    assigned), there is nothing to roll back here -- seed_goal_and_project
    already deletes the goal it created if the project create fails."""
    client.admin.create_goal = AsyncMock(side_effect=RuntimeError("upstream down"))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.admin.delete_project.assert_not_awaited()
    client.admin.delete_goal.assert_not_awaited()
