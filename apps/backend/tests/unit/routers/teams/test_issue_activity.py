"""Teams BFF — GET /teams/issues/{id}/activity (fence, id pattern, redaction)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext

ISSUE = "11111111-1111-1111-1111-111111111111"
H = {"Authorization": "Bearer x"}
URL = f"/api/v1/teams/issues/{ISSUE}/activity"


@pytest.fixture
def admin(monkeypatch):
    from routers.teams import agents as agents_mod

    ctx = TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu",
        session_cookie="cookie",
    )

    async def fake_ctx():
        return ctx

    fake = MagicMock()
    fake.get_issue = AsyncMock(return_value={"id": ISSUE, "companyId": "co_abc"})
    fake.list_issue_activity = AsyncMock(return_value=[])
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


client = TestClient(app, raise_server_exceptions=False)


def test_forwards_to_the_fixed_upstream_route_without_params(admin):
    admin.list_issue_activity.return_value = [
        {
            "id": "e1",
            "action": "issue.cross_issue_influence_cap_rejected",
            "actorType": "agent",
            "agentId": "a1",
            "details": {"identifier": "LB-1", "cap": 20, "count": 21},
        }
    ]
    r = client.get(f"{URL}?limit=9999&companyId=co_OTHER", headers=H)
    assert r.status_code == 200
    assert r.json()[0]["details"] == {"identifier": "LB-1", "cap": 20, "count": 21}
    admin.list_issue_activity.assert_awaited_once_with(issue_id=ISSUE, session_cookie="cookie")


def test_another_companys_issue_is_404_and_never_forwarded(admin):
    admin.get_issue.return_value = {"id": ISSUE, "companyId": "co_OTHER"}
    assert client.get(URL, headers=H).status_code == 404
    admin.list_issue_activity.assert_not_awaited()


def test_smuggled_issue_id_is_422(admin):
    r = client.get(f"/api/v1/teams/issues/{ISSUE}%3Fx=1%26/activity", headers=H)
    assert r.status_code == 422
    admin.get_issue.assert_not_awaited()
    admin.list_issue_activity.assert_not_awaited()


def test_redacts_secret_and_config_shaped_details(admin):
    admin.list_issue_activity.return_value = [
        {
            "id": "e1",
            "action": "issue.updated",
            "details": {
                "status": "done",
                "assigneeAdapterOverrides": {"adapterConfig": {"apiKey": "sk-1"}},
                "_previous": {"adapterConfig": {"authToken": "t"}, "status": "todo"},
                "providerTrace": {"raw": "x"},
                "costCents": 12,
                "inputTokens": 5,
                "budgetMonthlyCents": 100,
                "webhookSecret": "s",
                "nested": [{"password": "p", "keep": 1}],
            },
        }
    ]
    r = client.get(URL, headers=H)
    assert r.status_code == 200
    assert r.json()[0]["details"] == {
        "status": "done",
        "_previous": {"status": "todo"},
        "nested": [{"keep": 1}],
    }


def test_bounds_the_result_and_drops_non_dicts(admin):
    admin.list_issue_activity.return_value = [{"id": str(i)} for i in range(250)] + ["junk"]
    r = client.get(URL, headers=H)
    assert len(r.json()) == 200


def test_non_list_payload_is_empty(admin):
    admin.list_issue_activity.return_value = {"error": "x"}
    assert client.get(URL, headers=H).json() == []
