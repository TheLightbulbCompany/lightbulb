"""Teams BFF — scheduled-retry retry-now (the ported IssueScheduledRetryCard's route)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.services import trial_cap
from main import app
from routers.teams.deps import TeamsContext

ISSUE = "11111111-1111-1111-1111-111111111111"
H = {"Authorization": "Bearer x"}
URL = f"/api/v1/teams/issues/{ISSUE}/scheduled-retry/retry-now"


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
    fake.retry_scheduled_retry_now = AsyncMock(
        return_value={"outcome": "promoted", "message": "Retry promoted", "scheduledRetry": None}
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


client = TestClient(app, raise_server_exceptions=False)


def test_forwards_with_member_session(admin):
    r = client.post(URL, headers=H)
    assert r.status_code == 200
    assert r.json()["outcome"] == "promoted"
    admin.retry_scheduled_retry_now.assert_awaited_once_with(issue_id=ISSUE, session_cookie="cookie")


def test_another_companys_issue_is_404_and_never_forwarded(admin):
    admin.get_issue.return_value = {"id": ISSUE, "companyId": "co_OTHER"}
    assert client.post(URL, headers=H).status_code == 404
    admin.retry_scheduled_retry_now.assert_not_awaited()


def test_over_trial_cap_is_402_and_never_forwarded(admin, monkeypatch):
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))
    r = client.post(URL, headers=H)
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "trial_daily_cap"
    admin.retry_scheduled_retry_now.assert_not_awaited()
