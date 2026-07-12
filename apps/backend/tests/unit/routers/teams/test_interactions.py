"""Tests for the Teams issue-thread interactions BFF.

The four resolution routes (accept / reject / respond / cancel) are board-only
+ company-scoped against the SESSION ACTOR upstream, so they must proxy with
the per-user ``ctx.session_cookie`` — every test below asserts the admin-client
method is awaited with ``session_cookie="cookie"`` (the per-user session on the
fixture context), never an admin cookie.

Spec §5: each body is whitelisted with ``extra="forbid"``, so any smuggled
field (e.g. ``adapterType``) is rejected with 422 at the FastAPI boundary.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        session_cookie="cookie",
    )


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


def _patch_admin(monkeypatch, admin):
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)


def test_list_interactions_envelopes_and_proxies_session(client, monkeypatch):
    """GET wraps the bare upstream array in ``{interactions: [...]}`` and
    forwards the per-user session cookie + issue id."""
    admin = MagicMock()
    admin.list_issue_interactions = AsyncMock(return_value=[{"id": "int_1", "kind": "ask_user_questions"}])
    _patch_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/issues/iss_1/interactions",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"interactions": [{"id": "int_1", "kind": "ask_user_questions"}]}
    admin.list_issue_interactions.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


def test_accept_forwards_selected_keys(client, monkeypatch):
    admin = MagicMock()
    admin.accept_interaction = AsyncMock(return_value={"status": "accepted"})
    _patch_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/accept",
        json={"selectedClientKeys": ["k1", "k2"], "selectedOptionIds": ["o1"]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.accept_interaction.assert_awaited_once_with(
        issue_id="iss_1",
        interaction_id="int_1",
        body={"selectedClientKeys": ["k1", "k2"], "selectedOptionIds": ["o1"]},
        session_cookie="cookie",
    )


def test_accept_empty_body_accepts_defaults(client, monkeypatch):
    """An empty ``{}`` body is valid (accept all/defaults) and forwards ``{}``."""
    admin = MagicMock()
    admin.accept_interaction = AsyncMock(return_value={"status": "accepted"})
    _patch_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/accept",
        json={},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.accept_interaction.assert_awaited_once_with(
        issue_id="iss_1",
        interaction_id="int_1",
        body={},
        session_cookie="cookie",
    )


def test_reject_forwards_reason(client, monkeypatch):
    admin = MagicMock()
    admin.reject_interaction = AsyncMock(return_value={"status": "rejected"})
    _patch_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/reject",
        json={"reason": "not now"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.reject_interaction.assert_awaited_once_with(
        issue_id="iss_1",
        interaction_id="int_1",
        body={"reason": "not now"},
        session_cookie="cookie",
    )


def test_respond_forwards_answers_camelcase(client, monkeypatch):
    """``respond`` forwards the nested answers serialized to the camelCase
    shape upstream expects (``questionId`` / ``optionIds`` / ``otherText``)."""
    admin = MagicMock()
    admin.respond_interaction = AsyncMock(return_value={"status": "responded"})
    _patch_admin(monkeypatch, admin)
    from routers.teams import interactions as interactions_mod

    monkeypatch.setattr(interactions_mod.trial_cap, "is_over_daily_cap", AsyncMock(return_value=False))

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/respond",
        json={
            "answers": [
                {"questionId": "q1", "optionIds": ["a", "b"], "otherText": "more"},
                {"questionId": "q2", "optionIds": []},
            ],
            "summaryMarkdown": "## done",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.respond_interaction.assert_awaited_once_with(
        issue_id="iss_1",
        interaction_id="int_1",
        body={
            "answers": [
                {"questionId": "q1", "optionIds": ["a", "b"], "otherText": "more"},
                {"questionId": "q2", "optionIds": []},
            ],
            "summaryMarkdown": "## done",
        },
        session_cookie="cookie",
    )


def test_cancel_forwards_reason(client, monkeypatch):
    admin = MagicMock()
    admin.cancel_interaction = AsyncMock(return_value={"status": "cancelled"})
    _patch_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/cancel",
        json={"reason": "changed my mind"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.cancel_interaction.assert_awaited_once_with(
        issue_id="iss_1",
        interaction_id="int_1",
        body={"reason": "changed my mind"},
        session_cookie="cookie",
    )


def test_respond_requires_answers(client):
    """``answers`` is required by ``RespondInteractionBody``; missing it must 422."""
    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/respond",
        json={"summaryMarkdown": "no answers"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_accept_rejects_smuggled_field(client):
    """A client smuggling ``adapterType`` through the accept payload must be
    rejected with 422 — ``extra="forbid"``."""
    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/accept",
        json={"selectedClientKeys": ["k1"], "adapterType": "process"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_reject_rejects_smuggled_field(client):
    """Unknown field on the reject body is 422 (``extra="forbid"``)."""
    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/reject",
        json={"reason": "no", "url": "http://evil"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_respond_blocked_over_cap(client, monkeypatch):
    """A trial_local owner over the free-tier $3/day Meta cap gets a 402
    with the structured meta_daily_cap payload -- BEFORE the request ever
    reaches Paperclip (responding to an ask_user_questions interaction
    triggers a fresh Friday run server-side, so the check must run ahead of
    the proxy call to actually stop the spend)."""
    admin = MagicMock()
    admin.respond_interaction = AsyncMock(return_value={"status": "responded"})
    _patch_admin(monkeypatch, admin)
    from routers.teams import interactions as interactions_mod

    monkeypatch.setattr(interactions_mod.trial_cap, "is_over_daily_cap", AsyncMock(return_value=True))

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/respond",
        json={"answers": [{"questionId": "q1", "optionIds": ["a"]}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body == {
        "blocked": True,
        "code": "meta_daily_cap",
        "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    }
    admin.respond_interaction.assert_not_awaited()


def test_respond_proceeds_under_cap(client, monkeypatch):
    """An under-cap (or paid) owner's response proceeds to Paperclip as
    normal."""
    admin = MagicMock()
    admin.respond_interaction = AsyncMock(return_value={"status": "responded"})
    _patch_admin(monkeypatch, admin)
    from routers.teams import interactions as interactions_mod

    monkeypatch.setattr(interactions_mod.trial_cap, "is_over_daily_cap", AsyncMock(return_value=False))

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/respond",
        json={"answers": [{"questionId": "q1", "optionIds": ["a"]}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.respond_interaction.assert_awaited_once_with(
        issue_id="iss_1",
        interaction_id="int_1",
        body={"answers": [{"questionId": "q1", "optionIds": ["a"]}]},
        session_cookie="cookie",
    )
