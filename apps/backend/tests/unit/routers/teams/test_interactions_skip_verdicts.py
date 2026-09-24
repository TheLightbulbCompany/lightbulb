"""Teams BFF: interaction skip + item verdicts (task chat part B2).

Each route is fenced to the workspace's company (404 on a foreign issue, with
nothing forwarded), pins both ids to ``^[A-Za-z0-9_-]+$`` (422), whitelists its
body with ``extra="forbid"``, and forwards the per-user session cookie to a
fixed upstream path. Verdicts wake the agent, so they are trial-gated; skip
does not wake anything upstream, so it is not.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.services import trial_cap
from main import app
from routers.teams.deps import TeamsContext

H = {"Authorization": "Bearer x"}
SKIP = "/api/v1/teams/issues/iss_1/interactions/int_1/skip"
VERDICTS = "/api/v1/teams/issues/iss_1/interactions/int_1/verdicts"
GOOD_VERDICTS = {"verdicts": [{"id": "a", "verdict": "approve"}, {"id": "b", "verdict": "reject", "reason": "no"}]}


@pytest.fixture
def client():
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return TeamsContext(
            user_id="u1",
            org_id="o1",
            owner_id="o1",
            company_id="co_abc",
            paperclip_user_id="pcu_xyz",
            session_cookie="cookie",
        )

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


@pytest.fixture
def admin(monkeypatch):
    from routers.teams import agents as agents_mod

    fake = MagicMock()
    fake.get_issue = AsyncMock(return_value={"id": "iss_1", "companyId": "co_abc"})
    fake.skip_interaction = AsyncMock(return_value={"id": "int_1", "status": "cancelled"})
    fake.submit_interaction_verdicts = AsyncMock(return_value={"id": "int_1", "status": "pending"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))
    return fake


def test_skip_forwards_reason_with_session(client, admin):
    r = client.post(SKIP, json={"reason": "not now"}, headers=H)
    assert r.status_code == 200
    admin.skip_interaction.assert_awaited_once_with(
        issue_id="iss_1", interaction_id="int_1", body={"reason": "not now"}, session_cookie="cookie"
    )


def test_skip_empty_body_is_what_stock_sends(client, admin):
    r = client.post(SKIP, json={}, headers=H)
    assert r.status_code == 200
    assert admin.skip_interaction.await_args.kwargs["body"] == {}


def test_skip_is_not_trial_gated(client, admin, monkeypatch):
    """Upstream's skip enqueues no continuation — a capped trial may still skip."""
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))
    assert client.post(SKIP, json={}, headers=H).status_code == 200


def test_verdicts_forward_camel_body_with_session(client, admin):
    r = client.post(VERDICTS, json=GOOD_VERDICTS, headers=H)
    assert r.status_code == 200
    admin.submit_interaction_verdicts.assert_awaited_once_with(
        issue_id="iss_1", interaction_id="int_1", body=GOOD_VERDICTS, session_cookie="cookie"
    )


def test_verdicts_trial_gated_before_forwarding(client, admin, monkeypatch):
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))
    r = client.post(VERDICTS, json=GOOD_VERDICTS, headers=H)
    assert r.status_code == 402
    admin.submit_interaction_verdicts.assert_not_awaited()


@pytest.mark.real_fences
@pytest.mark.parametrize("url,body", [(SKIP, {}), (VERDICTS, GOOD_VERDICTS)])
def test_foreign_company_issue_404s_without_forwarding(client, admin, url, body):
    admin.get_issue = AsyncMock(return_value={"id": "iss_1", "companyId": "co_other"})
    r = client.post(url, json=body, headers=H)
    assert r.status_code == 404
    admin.skip_interaction.assert_not_awaited()
    admin.submit_interaction_verdicts.assert_not_awaited()


@pytest.mark.parametrize("verb,body", [("skip", {}), ("verdicts", GOOD_VERDICTS)])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/teams/issues/iss_1%3Fmode=x%26/interactions/int_1/{verb}",
        "/api/v1/teams/issues/iss_1/interactions/int_1%3Fx=1/{verb}",
    ],
)
def test_bad_ids_422(client, admin, verb, body, path):
    r = client.post(path.format(verb=verb), json=body, headers=H)
    assert r.status_code == 422
    admin.get_issue.assert_not_awaited()


@pytest.mark.parametrize(
    "url,body",
    [
        (SKIP, {"reason": "x", "adapterType": "process"}),
        (SKIP, {"reason": "x" * 4001}),
        (VERDICTS, {"verdicts": []}),
        (VERDICTS, {"verdicts": [{"id": "a", "verdict": "maybe"}]}),
        (VERDICTS, {"verdicts": [{"id": "a", "verdict": "approve", "resolvedByUserId": "u"}]}),
        (VERDICTS, {"verdicts": [{"id": "a", "verdict": "approve"}, {"id": "a", "verdict": "reject"}]}),
        (VERDICTS, {"verdicts": [{"id": "a", "verdict": "approve"}], "complete": True}),
    ],
)
def test_bodies_rejected(client, admin, url, body):
    r = client.post(url, json=body, headers=H)
    assert r.status_code == 422
    admin.skip_interaction.assert_not_awaited()
    admin.submit_interaction_verdicts.assert_not_awaited()
