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

from core.services import trial_cap

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
    # Every resolution route is gated on the trial predicate, so the default for
    # a happy-path test is "this owner has runway". A test that wants the block
    # overrides this afterwards. Without a stub the guard does a real DDB read.
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))


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


def test_list_interactions_enriches_human_actors(client, monkeypatch):
    """Human ``createdByUserId`` / ``resolvedByUserId`` / suggest_tasks draft
    ``assigneeUserId`` get ``*Email`` / ``*ClerkUserId`` stamped via the
    principal→Clerk map; agent-actor rows are left untouched."""
    admin = MagicMock()
    admin.list_issue_interactions = AsyncMock(
        return_value=[
            {
                "id": "int_1",
                "kind": "request_confirmation",
                "createdByAgentId": "agt_1",
                "resolvedByUserId": "pcu_xyz",
            },
            {
                "id": "int_2",
                "kind": "suggest_tasks",
                "createdByAgentId": "agt_1",
                "payload": {"version": 1, "tasks": [{"clientKey": "k1", "assigneeUserId": "pcu_xyz"}]},
            },
        ]
    )
    _patch_admin(monkeypatch, admin)
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "build_principal_to_clerk", AsyncMock(return_value={"pcu_xyz": "u1"}))
    monkeypatch.setattr(agents_mod, "_resolve_user_email", AsyncMock(return_value="ada@example.com"))

    r = client.get(
        "/api/v1/teams/issues/iss_1/interactions",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    rows = r.json()["interactions"]
    assert rows[0]["resolvedByEmail"] == "ada@example.com"
    assert rows[0]["resolvedByClerkUserId"] == "u1"
    assert "createdByEmail" not in rows[0]
    task = rows[1]["payload"]["tasks"][0]
    assert task["assigneeEmail"] == "ada@example.com"
    assert task["assigneeClerkUserId"] == "u1"


def test_list_interactions_survives_enrichment_blowup(client, monkeypatch):
    """Enrichment must never take down the thread — an unexpected failure
    anywhere inside it returns the upstream rows verbatim."""
    admin = MagicMock()
    admin.list_issue_interactions = AsyncMock(
        return_value=[{"id": "int_1", "kind": "request_confirmation", "resolvedByUserId": "pcu_xyz"}]
    )
    _patch_admin(monkeypatch, admin)
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(
        agents_mod,
        "build_principal_to_clerk",
        AsyncMock(side_effect=RuntimeError("map blew up")),
    )

    r = client.get(
        "/api/v1/teams/issues/iss_1/interactions",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "interactions": [{"id": "int_1", "kind": "request_confirmation", "resolvedByUserId": "pcu_xyz"}]
    }


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

    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))

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
    """A trial_local owner over the free-tier $3/day $3/day trial cap gets a 402
    with the structured trial_daily_cap payload -- BEFORE the request ever
    reaches Paperclip (responding to an ask_user_questions interaction
    triggers a fresh Friday run server-side, so the check must run ahead of
    the proxy call to actually stop the spend)."""
    admin = MagicMock()
    admin.respond_interaction = AsyncMock(return_value={"status": "responded"})
    _patch_admin(monkeypatch, admin)

    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))

    r = client.post(
        "/api/v1/teams/issues/iss_1/interactions/int_1/respond",
        json={"answers": [{"questionId": "q1", "optionIds": ["a"]}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body == {
        "blocked": True,
        "code": "trial_daily_cap",
        "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    }
    admin.respond_interaction.assert_not_awaited()


def test_respond_proceeds_under_cap(client, monkeypatch):
    """An under-cap (or paid) owner's response proceeds to Paperclip as
    normal."""
    admin = MagicMock()
    admin.respond_interaction = AsyncMock(return_value={"status": "responded"})
    _patch_admin(monkeypatch, admin)

    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))

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


# ---------------------------------------------------------------------------
# EVERY resolution route is a spend door, not just `respond`.
#
# Upstream Paperclip's accept / reject / respond / cancel routes ALL call
# queueResolvedInteractionContinuationWakeup (see server/src/routes/issues.ts) —
# the agent is blocked waiting on a human answer, and "no" and "cancel" are
# answers too: each one resumes the run and costs model tokens. Gating only
# `respond` left three ungated doors into Bedrock spend.

_RESOLUTION_ROUTES = [
    ("accept", {}),
    ("reject", {"reason": "no thanks"}),
    ("respond", {"answers": [{"questionId": "q1", "optionIds": ["a"]}]}),
    ("cancel", {"reason": "never mind"}),
]


@pytest.mark.parametrize("route,body", _RESOLUTION_ROUTES)
@pytest.mark.parametrize(
    "reason,code",
    [("trial_daily_cap", "trial_daily_cap"), ("trial_expired", "trial_expired")],
)
def test_every_interaction_resolution_route_is_gated(client, monkeypatch, route, body, reason, code):
    admin = MagicMock()
    for name in ("accept_interaction", "reject_interaction", "respond_interaction", "cancel_interaction"):
        setattr(admin, name, AsyncMock(return_value={"status": "ok"}))
    _patch_admin(monkeypatch, admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=reason))

    r = client.post(
        f"/api/v1/teams/issues/iss_1/interactions/int_1/{route}",
        json=body,
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 402, f"{route} is an ungated spend door"
    assert r.json()["detail"]["code"] == code
    # The wake never reached Paperclip — there is no enforcement point downstream.
    getattr(admin, f"{route}_interaction").assert_not_awaited()
