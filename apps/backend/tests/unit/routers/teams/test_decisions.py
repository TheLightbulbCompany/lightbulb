# apps/backend/tests/unit/routers/teams/test_decisions.py
"""Teams BFF — decisions (read one, decide, dismiss).

Mirrors the fixture/mocking style of the sibling teams tests
(test_feedback.py, test_interactions.py). The company fence here is live in
every test: ``get_decision`` is both the fence and the read.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from core.services import trial_cap
from main import app
from routers.teams.deps import TeamsContext

AUTH = {"Authorization": "Bearer x"}


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu",
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


def _admin(monkeypatch, decision):
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.get_decision = AsyncMock(return_value=decision)
    admin.list_decisions = AsyncMock(return_value=[])
    admin.decide_decision = AsyncMock(return_value={**decision, "status": "decided", "executions": []})
    admin.dismiss_decision = AsyncMock(return_value={**decision, "status": "decided", "executions": []})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    # decide/dismiss are gated on the trial predicate; without a stub the guard
    # does a real DDB read. A test that wants the block overrides this.
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))
    return admin


OPEN = {"id": "dec1", "companyId": "co_abc", "status": "open", "executions": []}


def test_get_open_decision_is_stamped_with_target_changed(client, monkeypatch):
    admin = _admin(monkeypatch, OPEN)
    admin.list_decisions = AsyncMock(
        return_value=[
            {"id": "other", "targetChanged": {"iss9": True}},
            {"id": "dec1", "targetChanged": {"iss1": True, "iss2": False}},
        ]
    )

    r = client.get("/api/v1/teams/decisions/dec1", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {**OPEN, "targetChanged": {"iss1": True, "iss2": False}}
    admin.get_decision.assert_awaited_once_with(decision_id="dec1", session_cookie="cookie")
    admin.list_decisions.assert_awaited_once_with(company_id="co_abc", status="open", session_cookie="cookie")


def test_get_open_decision_missing_from_the_open_page_is_returned_bare(client, monkeypatch):
    _admin(monkeypatch, OPEN)

    r = client.get("/api/v1/teams/decisions/dec1", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == OPEN


def test_get_decided_decision_skips_the_open_list(client, monkeypatch):
    decided = {**OPEN, "status": "decided", "executions": [{"id": "ex1", "status": "executed"}]}
    admin = _admin(monkeypatch, decided)

    r = client.get("/api/v1/teams/decisions/dec1", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == decided
    admin.list_decisions.assert_not_awaited()


def test_decide_forwards_whitelisted_camelcase_body(client, monkeypatch):
    admin = _admin(monkeypatch, OPEN)

    r = client.post(
        "/api/v1/teams/decisions/dec1/decide",
        json={"optionId": "ship", "inputValues": {"note": "go"}, "idempotencyKey": "k1"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "decided"
    admin.decide_decision.assert_awaited_once_with(
        decision_id="dec1",
        body={"optionId": "ship", "inputValues": {"note": "go"}, "idempotencyKey": "k1"},
        session_cookie="cookie",
    )


def test_decide_requires_option_id_and_rejects_smuggled_fields(client, monkeypatch):
    admin = _admin(monkeypatch, OPEN)

    missing = client.post("/api/v1/teams/decisions/dec1/decide", json={}, headers=AUTH)
    smuggled = client.post(
        "/api/v1/teams/decisions/dec1/decide",
        json={"optionId": "ship", "decidedByUserId": "someone-else"},
        headers=AUTH,
    )
    assert missing.status_code == 422
    assert smuggled.status_code == 422
    admin.decide_decision.assert_not_awaited()


def test_dismiss_forwards_reason(client, monkeypatch):
    admin = _admin(monkeypatch, OPEN)

    r = client.post("/api/v1/teams/decisions/dec1/dismiss", json={"reason": "not now"}, headers=AUTH)
    assert r.status_code == 200
    admin.dismiss_decision.assert_awaited_once_with(
        decision_id="dec1", body={"reason": "not now"}, session_cookie="cookie"
    )


def test_dismiss_without_reason_forwards_empty_body(client, monkeypatch):
    admin = _admin(monkeypatch, OPEN)

    r = client.post("/api/v1/teams/decisions/dec1/dismiss", json={}, headers=AUTH)
    assert r.status_code == 200
    assert admin.dismiss_decision.await_args.kwargs["body"] == {}


@pytest.mark.parametrize(
    "method,url",
    [
        ("get", "/decisions/dec1"),
        ("post", "/decisions/dec1/decide"),
        ("post", "/decisions/dec1/dismiss"),
    ],
)
def test_decision_from_another_company_is_404_and_never_forwarded(client, monkeypatch, method, url):
    admin = _admin(monkeypatch, {**OPEN, "companyId": "co_other"})

    r = client.request(method.upper(), f"/api/v1/teams{url}", json={"optionId": "ship"}, headers=AUTH)
    assert r.status_code == 404
    admin.list_decisions.assert_not_awaited()
    admin.decide_decision.assert_not_awaited()
    admin.dismiss_decision.assert_not_awaited()


@pytest.mark.parametrize("verb,body", [("decide", {"optionId": "ship"}), ("dismiss", {})])
def test_resolving_is_blocked_without_trial_runway(client, monkeypatch, verb, body):
    admin = _admin(monkeypatch, OPEN)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))

    r = client.post(f"/api/v1/teams/decisions/dec1/{verb}", json=body, headers=AUTH)
    assert r.status_code == 402
    admin.decide_decision.assert_not_awaited()
    admin.dismiss_decision.assert_not_awaited()
