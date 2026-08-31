"""Router tests for POST /api/v1/onboarding/brief.

The endpoint is INTAKE ONLY: it records the funnel answer under the owner id
and returns. Fulfilment (funnel claim, goal/project seed, BRIEF.md, Friday's
kickoff) lives in ``core.services.onboarding_brief.reconcile_pending_brief``
and is covered by tests/unit/services/test_onboarding_brief_reconcile.py.

The most load-bearing thing this file asserts is a NEGATIVE: the fixture
wires up no Teams context, no Paperclip admin client and no workspace, and
every request still succeeds. That is the whole point of the split -- the
funnel renders while the workspace is still provisioning, so an endpoint that
needed any of those would 202 and put the retry burden back on the browser.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth import AuthContext, get_current_user
import routers.onboarding as onboarding_mod


def _personal_auth():
    return AuthContext(user_id="user_abc", org_id=None, email="max@acme.com")


def _org_auth():
    return AuthContext(user_id="user_abc", org_id="org_xyz", org_role="admin", email="max@acme.com")


@pytest.fixture
def client(monkeypatch):
    repo = MagicMock()
    repo.put_pending_brief = AsyncMock(return_value=True)
    monkeypatch.setattr(onboarding_mod, "PaperclipRepo", lambda **kwargs: repo)

    # Fire-and-forget (asyncio.create_task) -- an AsyncMock keeps it off real
    # DDB/Paperclip state. Tests that care assert against `c.reconcile`.
    reconcile = AsyncMock()
    monkeypatch.setattr(onboarding_mod, "reconcile_pending_brief", reconcile)
    nudge = MagicMock()
    monkeypatch.setattr(onboarding_mod, "publish_status_changed", nudge)

    # Defaults to "has a container" -- the ordinary onboarding path -- so
    # existing already-submitted tests exercise the SAME refusal they always
    # have without touching real DDB. The held-owner upsert tests below
    # override this to None.
    get_container = AsyncMock(return_value={"owner_id": "x"})
    monkeypatch.setattr(onboarding_mod.container_repo, "get_by_owner_id", get_container)

    app.dependency_overrides[get_current_user] = _personal_auth
    with TestClient(app) as c:
        c.repo = repo
        c.put = repo.put_pending_brief
        c.reconcile = reconcile
        c.nudge = nudge
        c.get_container = get_container
        yield c
    app.dependency_overrides.clear()


def test_submit_records_the_answer_and_returns_staged(client):
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "grow", "input": "https://acme.com", "detail": "Austin builds"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "staged"}
    client.put.assert_awaited_once_with(
        "user_abc",  # resolve_owner_id(auth) -- personal owner
        path="grow",
        user_input="https://acme.com",
        detail="Austin builds",
        # The user's own words, built at intake so the phrasing that reaches
        # Friday is the one this submission produced (grow path -> the
        # registrable domain, not the raw URL).
        brief_text="work on acme.com",
        submitted_by="user_abc",
    )


def test_intake_needs_nothing_but_auth(client):
    """No Teams context, no Paperclip, no workspace, no owner row.

    Nothing Paperclip-shaped is stubbed in the fixture, so if any of it came
    back as a dependency this request would fail rather than 200. ``_ctx`` is
    asserted gone by name too -- re-adding it is exactly the regression that
    would restore the 202-and-let-the-browser-retry behaviour.
    """
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    assert not hasattr(onboarding_mod, "_ctx")
    assert not hasattr(onboarding_mod, "get_workspace")


def test_org_owner_is_keyed_on_the_org_not_the_member(client):
    """Onboarding is company-scoped: an org's pending brief must be keyed on
    the org id, or two members would each get their own."""
    app.dependency_overrides[get_current_user] = _org_auth

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    assert client.put.await_args.args[0] == "org_xyz"
    # ...while submitted_by stays the ACTUAL member, which is who the kickoff
    # is eventually addressed at.
    assert client.put.await_args.kwargs["submitted_by"] == "user_abc"


def test_second_submit_for_same_company_is_already_submitted(client):
    """Put-if-absent lost: a peer member (or a double-tap) got there first."""
    client.put.return_value = False

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_submitted"}
    client.nudge.assert_not_called()


def test_a_duplicate_submit_still_fires_the_drain(client):
    """Re-submitting is the ONLY lever the funnel gives a user whose brief is
    sitting undrained, so the path where a retry is most wanted must not be
    the one that skips it. The reconciler dedupes concurrent drains itself."""
    client.put.return_value = False

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.json() == {"status": "already_submitted"}
    client.reconcile.assert_awaited_once_with("user_abc")


def test_submit_fires_the_drain_and_nudges_tabs(client):
    """The drain runs in case the workspace is ALREADY up (making the split
    invisible); the nudge lets every tab on this company drop the funnel
    without waiting out the ~3s poll."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "An agency"})

    assert resp.status_code == 200
    client.reconcile.assert_awaited_once_with("user_abc")
    client.nudge.assert_called_once_with("user_abc")


def test_intake_emits_no_conversion_event(client):
    """`first_run_brief_submitted` belongs to the drain, once the goal and
    project actually exist (core/services/onboarding_brief.py). Emitting it
    here would report a 100% onboarding funnel straight through a fleet-wide
    fulfilment outage -- the one signal that would surface it."""
    assert not hasattr(onboarding_mod, "capture_product_event")


# --- "I don't know yet — you pick": submit with no input ---------------------


def test_new_path_without_input_is_recorded_as_no_idea(client):
    """The absent idea IS the signal -- it must survive intake as None, not
    as a blank string a later reader would quote back at Friday."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new"})

    assert resp.status_code == 200
    kwargs = client.put.await_args.kwargs
    assert kwargs["user_input"] is None
    assert kwargs["brief_text"] is None


def test_new_path_with_blank_input_is_treated_as_no_idea(client):
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "   "})

    assert resp.status_code == 200
    assert client.put.await_args.kwargs["user_input"] is None


def test_grow_path_still_requires_input(client):
    """Only `new` may omit the idea -- a grow brief with no website is useless."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "grow"})

    assert resp.status_code == 422
    client.put.assert_not_awaited()


def test_too_short_input_still_rejected(client):
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "ab"})

    assert resp.status_code == 422
    client.put.assert_not_awaited()


# --- editing a stashed idea before the container exists -----------------------


def test_pre_container_owner_resubmit_upserts_the_pending_brief(client):
    """No container row yet + a brief already pending == the landing card's
    "edit your idea" flow re-POSTing this route. That must actually replace
    the stored text, not bounce off as already_submitted."""
    client.get_container.return_value = None  # no container yet
    client.put.side_effect = [False, True]  # first put loses the guard, retry overwrites

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "A better idea"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "staged"}
    assert client.put.await_count == 2
    retry_kwargs = client.put.await_args_list[1].kwargs
    assert retry_kwargs["overwrite"] is True
    assert retry_kwargs["user_input"] == "A better idea"
    client.nudge.assert_called_once_with("user_abc")


def test_owner_with_a_container_never_gets_the_upsert_retry(client):
    """The ordinary onboarding path is untouched: a container row present
    means this item is a one-shot claim ticket for the drain, so a second
    submit still refuses -- no overwrite attempt at all."""
    client.get_container.return_value = {"owner_id": "user_abc"}  # has a container
    client.put.return_value = False

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "A better idea"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_submitted"}
    client.put.assert_awaited_once()  # no retry
    client.nudge.assert_not_called()


def test_container_read_failure_keeps_the_original_refusal(client):
    """An unreadable container row must not flip a real onboarding owner into
    the overwrite branch -- degrade toward the existing refusal."""
    client.get_container.side_effect = RuntimeError("boom")
    client.put.return_value = False

    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "A better idea"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_submitted"}
    client.put.assert_awaited_once()
