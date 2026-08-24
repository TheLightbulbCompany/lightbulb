"""Tests for the Teams BFF unified attention feed."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext

HEADERS = {"Authorization": "Bearer x"}


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


def _admin(monkeypatch, **methods):
    """Install a MagicMock admin client with the given AsyncMock methods."""
    admin = MagicMock()
    admin.list_attention = AsyncMock(return_value={"items": []})
    admin.list_approvals = AsyncMock(return_value=[])
    admin.list_issue_interactions = AsyncMock(return_value=[])
    for name, value in methods.items():
        setattr(admin, name, AsyncMock(return_value=value))
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    return admin


def _interaction_item(interaction_id: str, issue_id: str) -> dict:
    return {
        "sourceKind": "issue_thread_interaction",
        "subject": {
            "kind": "interaction",
            "id": interaction_id,
            "metadata": {"issueId": issue_id},
        },
        "relatedIssue": {"id": issue_id},
    }


def _approval_item(approval_id: str) -> dict:
    return {
        "sourceKind": "approval",
        "subject": {"kind": "approval", "id": approval_id},
    }


def test_proxies_with_the_per_user_session_cookie(client, monkeypatch):
    """The feed is board-only and joins per-user dismissal state upstream, so
    it MUST ride the caller's own session cookie — an instance-admin cookie
    would cross tenants and resolve dismissals against the wrong user."""
    admin = _admin(monkeypatch)

    r = client.get("/api/v1/teams/attention", headers=HEADERS)

    assert r.status_code == 200
    admin.list_attention.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        params=None,
    )


def test_forwards_whitelisted_query_params(client, monkeypatch):
    admin = _admin(monkeypatch)

    r = client.get(
        "/api/v1/teams/attention?limit=10&sort=decide&includeDismissed=true",
        headers=HEADERS,
    )

    assert r.status_code == 200
    assert admin.list_attention.await_args.kwargs["params"] == {
        "limit": "10",
        "sort": "decide",
        "includeDismissed": "true",
    }


@pytest.mark.parametrize("query", ["sort=bogus", "limit=0", "limit=101"])
def test_rejects_bad_query_locally_without_calling_upstream(client, monkeypatch, query):
    """A malformed value is a local 422 — never an upstream 400 whose body can
    leak the internal company path."""
    admin = _admin(monkeypatch)

    r = client.get(f"/api/v1/teams/attention?{query}", headers=HEADERS)

    assert r.status_code == 422
    admin.list_attention.assert_not_awaited()


def test_hydrates_interaction_rows_so_cards_resolve_inline(client, monkeypatch):
    """The feed's own `detail` is a preview; the card needs the full row."""
    interaction = {
        "id": "int_1",
        "issueId": "iss_1",
        "kind": "request_confirmation",
        "status": "pending",
        "payload": {"prompt": "Ship it?"},
    }
    admin = _admin(monkeypatch)
    admin.list_attention.return_value = {
        "items": [_interaction_item("int_1", "iss_1")],
        "totalCount": 1,
    }
    admin.list_issue_interactions.return_value = [interaction]

    r = client.get("/api/v1/teams/attention", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["items"][0]["interaction"] == interaction
    admin.list_issue_interactions.assert_awaited_once_with(issue_id="iss_1", session_cookie="cookie")


def test_dedupes_interaction_reads_per_issue(client, monkeypatch):
    """Several pending interactions on one thread cost ONE upstream read."""
    admin = _admin(monkeypatch)
    admin.list_attention.return_value = {
        "items": [
            _interaction_item("int_1", "iss_1"),
            _interaction_item("int_2", "iss_1"),
        ]
    }
    admin.list_issue_interactions.return_value = [
        {"id": "int_1", "issueId": "iss_1", "kind": "request_confirmation"},
        {"id": "int_2", "issueId": "iss_1", "kind": "ask_user_questions"},
    ]

    r = client.get("/api/v1/teams/attention", headers=HEADERS)

    assert r.status_code == 200
    assert admin.list_issue_interactions.await_count == 1
    ids = [item["interaction"]["id"] for item in r.json()["items"]]
    assert ids == ["int_1", "int_2"]


def test_hydrates_approvals_with_one_read(client, monkeypatch):
    approval = {"id": "ap_1", "type": "hire_agent", "status": "pending"}
    admin = _admin(monkeypatch)
    admin.list_attention.return_value = {"items": [_approval_item("ap_1")]}
    admin.list_approvals.return_value = [approval]

    r = client.get("/api/v1/teams/attention", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["items"][0]["approval"] == approval
    assert admin.list_approvals.await_count == 1


def test_hydration_failure_still_serves_the_feed(client, monkeypatch):
    """Hydration is decoration over a feed that is already correct. A failing
    upstream read must leave the item unhydrated, not take the list down —
    the whole point is that decisions stop being invisible."""
    admin = _admin(monkeypatch)
    admin.list_attention.return_value = {
        "items": [_interaction_item("int_1", "iss_1")],
        "totalCount": 1,
    }
    admin.list_issue_interactions.side_effect = RuntimeError("upstream down")

    r = client.get("/api/v1/teams/attention", headers=HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert body["totalCount"] == 1
    assert "interaction" not in body["items"][0]


def test_malformed_upstream_body_returns_an_empty_feed_not_a_fake_count(client, monkeypatch):
    """Never fabricate a count: an unusable upstream body is reported as
    empty rather than guessed at."""
    admin = _admin(monkeypatch)
    admin.list_attention.return_value = []

    r = client.get("/api/v1/teams/attention", headers=HEADERS)

    assert r.status_code == 200
    assert r.json() == {"items": [], "totalCount": 0, "countsBySourceKind": {}}


def test_caps_interaction_fan_out(client, monkeypatch):
    """A pathological feed must not fan out one upstream call per row."""
    from routers.teams import attention as attention_mod

    admin = _admin(monkeypatch)
    admin.list_attention.return_value = {"items": [_interaction_item(f"int_{n}", f"iss_{n}") for n in range(60)]}

    r = client.get("/api/v1/teams/attention", headers=HEADERS)

    assert r.status_code == 200
    assert admin.list_issue_interactions.await_count == attention_mod._MAX_HYDRATED_ISSUES
