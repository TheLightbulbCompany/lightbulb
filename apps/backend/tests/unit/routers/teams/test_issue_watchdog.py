"""Teams BFF — task watchdog on an issue (the ported Watchdog row's routes)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.services import trial_cap
from main import app
from routers.teams.deps import TeamsContext

ISSUE = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"
H = {"Authorization": "Bearer x"}
URL = f"/api/v1/teams/issues/{ISSUE}/watchdog"


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
    fake.upsert_issue_watchdog = AsyncMock(return_value={"id": "wd_1", "watchdogAgentId": AGENT})
    fake.delete_issue_watchdog = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


client = TestClient(app, raise_server_exceptions=False)


def test_put_forwards_the_body_with_the_member_session(admin):
    r = client.put(URL, headers=H, json={"agentId": AGENT, "instructions": "Resume the release check."})
    assert r.status_code == 200
    assert r.json()["watchdogAgentId"] == AGENT
    admin.upsert_issue_watchdog.assert_awaited_once_with(
        issue_id=ISSUE,
        body={"agentId": AGENT, "instructions": "Resume the release check."},
        session_cookie="cookie",
    )


def test_put_forwards_an_explicit_null_to_clear_instructions(admin):
    assert client.put(URL, headers=H, json={"agentId": AGENT, "instructions": None}).status_code == 200
    assert admin.upsert_issue_watchdog.await_args.kwargs["body"] == {"agentId": AGENT, "instructions": None}


def test_put_rejects_smuggled_fields_and_a_non_uuid_agent(admin):
    assert client.put(URL, headers=H, json={"agentId": AGENT, "adapterType": "x"}).status_code == 422
    assert client.put(URL, headers=H, json={"agentId": "not-a-uuid"}).status_code == 422
    admin.upsert_issue_watchdog.assert_not_awaited()


def test_put_over_trial_cap_is_402_but_delete_still_works(admin, monkeypatch):
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))
    r = client.put(URL, headers=H, json={"agentId": AGENT})
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "trial_daily_cap"
    admin.upsert_issue_watchdog.assert_not_awaited()
    # Removing a watchdog starts nothing, so it is never gated.
    assert client.delete(URL, headers=H).status_code == 200
    admin.delete_issue_watchdog.assert_awaited_once()


def test_delete_forwards_with_the_member_session(admin):
    r = client.delete(URL, headers=H)
    assert r.status_code == 200
    admin.delete_issue_watchdog.assert_awaited_once_with(issue_id=ISSUE, session_cookie="cookie")


@pytest.mark.real_fences
def test_another_companys_issue_is_404_and_never_forwarded(admin):
    admin.get_issue.return_value = {"id": ISSUE, "companyId": "co_OTHER"}
    assert client.put(URL, headers=H, json={"agentId": AGENT}).status_code == 404
    assert client.delete(URL, headers=H).status_code == 404
    admin.upsert_issue_watchdog.assert_not_awaited()
    admin.delete_issue_watchdog.assert_not_awaited()
