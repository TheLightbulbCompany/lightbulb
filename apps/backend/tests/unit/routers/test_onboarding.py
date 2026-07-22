"""Router tests for POST /api/v1/onboarding/brief.

Pattern mirrors tests/unit/routers/teams/test_work.py: override the
TeamsContext dependency, monkeypatch _admin() with a MagicMock carrying
AsyncMock methods, and monkeypatch get_workspace with a MagicMock.

Onboarding is now one-shot per COMPANY (not per personal/trial owner):
the ``PaperclipRepo`` onboarding claim gates the seed, so ``client.claim``
/ ``client.release`` / ``client.record`` stand in for
``claim_onboarding`` / ``release_onboarding_claim`` / ``record_onboarding_ids``.
"""

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth import AuthContext, get_current_user
from core.repositories.paperclip_repo import OnboardingClaimResult
from core.services.paperclip_admin_client import PaperclipApiError
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
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(return_value={"id": "proj-1"})
    admin.delete_goal = AsyncMock()
    admin.delete_project = AsyncMock()
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    ws = MagicMock()
    ws.write_file = MagicMock()
    monkeypatch.setattr(onboarding_mod, "get_workspace", lambda: ws)

    repo = MagicMock()
    repo.claim_onboarding = AsyncMock(return_value=OnboardingClaimResult.WON)
    repo.release_onboarding_claim = AsyncMock()
    repo.record_onboarding_ids = AsyncMock()
    repo.get_onboarding = AsyncMock(return_value=None)
    monkeypatch.setattr(onboarding_mod, "PaperclipRepo", lambda **kwargs: repo)

    # The success path stamps onboarded_at on the container row (the
    # per-container funnel-done marker /container/status surfaces).
    mark_onboarded = AsyncMock()
    monkeypatch.setattr(onboarding_mod.container_repo, "update_fields", mark_onboarded)

    # Default: no first-contact token (agent email inactive / no active
    # address yet) -- the inert-safe default so existing tests are
    # unaffected. Individual tests override this to exercise the wiring.
    mint_first_contact = AsyncMock(return_value=None)
    monkeypatch.setattr(onboarding_mod, "mint_first_contact_token", mint_first_contact)

    app.dependency_overrides[get_current_user] = _personal_auth
    app.dependency_overrides[onboarding_mod._ctx] = lambda: ctx
    with TestClient(app) as c:
        c.admin = admin
        c.ws = ws
        c.repo = repo
        c.claim = repo.claim_onboarding
        c.release = repo.release_onboarding_claim
        c.record = repo.record_onboarding_ids
        c.get_onboarding = repo.get_onboarding
        c.mark_onboarded = mark_onboarded
        c.mint_first_contact = mint_first_contact
        yield c
    app.dependency_overrides.clear()


def test_success_stamps_onboarded_on_the_container(client):
    """The per-container funnel-done marker: stamped on the owner's container
    row so /container/status can report onboarded and the funnel never shows
    to a second org member on the same container."""
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "An agency for restaurants"},
    )

    assert resp.status_code == 200
    client.mark_onboarded.assert_awaited_once()
    call = client.mark_onboarded.await_args
    assert call.args[0] == "user_abc"  # ctx.owner_id (container owner)
    assert "onboarded_at" in call.args[1]


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
    assert brief_call.args[0] == "user_abc"
    assert brief_call.args[1] == "workspaces/friday-uuid/BRIEF.md"
    assert "https://acme.com" in brief_call.args[2]

    # The board is no longer seeded on submit — Friday fills it at the END of
    # her research session with the three next-move tasks she proposes.
    client.admin.create_issue.assert_not_called()


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


# --- Company-scoped onboarding claim + goal/project seeding ------------------


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


def test_same_holder_threads_through_claim_and_record(client):
    """The route generates one holder per request and must pass the SAME
    value to claim_onboarding and record_onboarding_ids -- record's guard
    (attribute_exists(user_id) AND holder = :h) would reject even the
    happy path if these ever drifted apart."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    claim_holder = client.claim.await_args.kwargs["holder"]
    record_holder = client.record.await_args.kwargs["holder"]
    assert claim_holder == record_holder
    client.record.assert_awaited_once_with("comp-1", goal_id="goal-1", project_id="proj-1", holder=claim_holder)


def test_second_submit_for_same_company_is_a_noop(client):
    """SEEDED is a durably-done company (seeded_at on a non-rolled-back claim)
    -- the goal + project exist, so a peer here is already_submitted."""
    client.claim.return_value = OnboardingClaimResult.SEEDED

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.json() == {"status": "already_submitted"}
    client.admin.create_goal.assert_not_awaited()
    client.ws.write_file.assert_not_called()


def test_in_flight_claim_is_a_retryable_409_not_already_submitted(client):
    """A peer's claim being merely in-flight (not yet SEEDED) must NOT be
    reported as already_submitted -- that peer's own submit could still
    fail, and this caller would then be permanently waved into a company
    with no goal or project. It must be a retryable 409 instead, matching
    the other not_ready cases the client already retries on."""
    client.claim.return_value = OnboardingClaimResult.IN_FLIGHT

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_ready"
    client.admin.create_goal.assert_not_awaited()
    client.ws.write_file.assert_not_called()
    # Not the caller's claim to release -- it never held one.
    client.release.assert_not_awaited()


def test_paperclip_not_ready_releases_the_claim(client):
    client.admin.list_agents = AsyncMock(side_effect=PaperclipApiError("boom", status_code=503, body=None))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.release.assert_awaited_once_with("comp-1", holder=ANY)


def test_seed_failure_releases_the_claim(client):
    client.admin.create_goal = AsyncMock(side_effect=RuntimeError("upstream down"))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.release.assert_awaited_once_with("comp-1", holder=ANY)
    client.ws.write_file.assert_not_called()


# --- Post-seed failure rolls back both seeded rows (FIX 1) -------------------


def test_post_seed_failure_rolls_back_goal_and_project_then_releases_claim(client):
    """Once seed_goal_and_project has returned ids, a later failure (e.g. a
    DynamoDB throttle on record_onboarding_ids, which is NOT in the caught
    (PaperclipApiError, httpx.HTTPError, RuntimeError) set and so escapes as
    a raw 500) must delete both seeded Paperclip rows before releasing the
    claim. Otherwise a client retry wins a fresh claim and seeds a SECOND
    goal/project pair for the same company."""
    client.record.side_effect = Exception("dynamodb throttled")

    with pytest.raises(Exception, match="dynamodb throttled"):
        client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    client.admin.delete_project.assert_awaited_once_with(session_cookie="cookie", project_id="proj-1")
    client.admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")
    client.release.assert_awaited_once_with("comp-1", holder=ANY)


def test_post_seed_cleanup_is_failure_tolerant_and_preserves_the_original_error(client):
    """A failure deleting the project must not skip the goal delete or the
    claim release, and the cleanup failure itself must not mask the
    ORIGINAL error the caller sees."""
    client.record.side_effect = Exception("dynamodb throttled")
    client.admin.delete_project.side_effect = Exception("project delete also failed")

    with pytest.raises(Exception, match="dynamodb throttled"):
        client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    client.admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")
    client.release.assert_awaited_once_with("comp-1", holder=ANY)


# --- Friday's first email (first-contact token wiring) -----------------------


def test_mints_first_contact_token_with_owner_id_and_email(client):
    """owner_id/owner_email come from ctx/auth, already in scope here."""
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "An agency"},
    )
    assert resp.status_code == 200
    client.mint_first_contact.assert_awaited_once()
    kwargs = client.mint_first_contact.await_args.kwargs
    assert kwargs["owner_id"] == "user_abc"  # ctx.owner_id
    assert kwargs["owner_email"] == "max@acme.com"  # auth.email
    assert isinstance(kwargs["subject"], str) and kwargs["subject"]


def test_bootstrap_carries_the_first_contact_token_when_minted(client):
    client.mint_first_contact.return_value = "TOKEN_XYZ"

    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "An agency"},
    )
    assert resp.status_code == 200

    bootstrap_call = client.ws.write_file.call_args_list[1]
    assert bootstrap_call.args[1] == "workspaces/friday-uuid/BOOTSTRAP.md"
    assert "TOKEN_XYZ" in bootstrap_call.args[2]
    assert "scripts/isol8-mail.sh" in bootstrap_call.args[2]


def test_bootstrap_omits_email_step_when_no_token_minted(client):
    """Inert-safe default: mint_first_contact_token returning None (agent
    email inactive, or no active address yet) must leave BOOTSTRAP.md with
    no email step at all."""
    client.mint_first_contact.return_value = None

    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "An agency"},
    )
    assert resp.status_code == 200

    bootstrap_call = client.ws.write_file.call_args_list[1]
    assert bootstrap_call.args[1] == "workspaces/friday-uuid/BOOTSTRAP.md"
    assert "scripts/isol8-mail.sh" not in bootstrap_call.args[2]


def test_seed_only_failure_does_not_attempt_rollback(client):
    """When seed_goal_and_project itself fails (goal_id/project_id never
    assigned), there is nothing to roll back here -- seed_goal_and_project
    already deletes the goal it created if the project create fails."""
    client.admin.create_goal = AsyncMock(side_effect=RuntimeError("upstream down"))

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 409
    client.admin.delete_project.assert_not_awaited()
    client.admin.delete_goal.assert_not_awaited()
