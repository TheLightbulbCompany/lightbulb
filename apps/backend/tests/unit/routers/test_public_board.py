import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from routers import public_board  # noqa: E402

PREFIX = "/api/v1/public"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_PUBLIC_BOARD_ENABLED", "true")
    monkeypatch.setenv("PAPERCLIP_PUBLIC_BOARD_API_KEY", "test-token")
    monkeypatch.setenv("PAPERCLIP_PUBLIC_BOARD_COMPANY_ID", "company-1")
    app = FastAPI()
    app.include_router(public_board.router, prefix=PREFIX)
    return TestClient(app)


def test_public_board_is_disabled_until_founder_publication(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_PUBLIC_BOARD_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(public_board.router, prefix=PREFIX)
    resp = TestClient(app).get(f"{PREFIX}/board")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "public board not published"


def test_public_board_projection_is_read_only_and_sanitized(client, monkeypatch):
    calls = []

    async def fake_fetch(_client, path, *, params=None):
        calls.append((path, params))
        if path.endswith("/issues"):
            return {
                "issues": [
                    {
                        "id": "issue-1",
                        "identifier": "ISOA-116",
                        "title": "Publish board",
                        "description": "private task body must not leak",
                        "status": "in_progress",
                        "priority": "high",
                        "adapterConfig": {"secret": "nope"},
                        "assigneeAgentId": "agent-1",
                        "responsibleUserId": "user-1",
                    }
                ]
            }
        if path == "/api/issues/issue-1/work-products":
            return {
                "workProducts": [
                    {"id": "wp-1", "title": "PR", "url": "https://github.com/acme/repo/pull/1", "secret": "hide"},
                    {"id": "wp-2", "title": "internal", "url": "http://paperclip.isol8-prod.local/private"},
                ]
            }
        if path.endswith("/routines"):
            return {"routines": [{"id": "routine-1", "title": "Friday review", "prompt": "private", "reviewerAgentId": "agent-2"}]}
        if path.endswith("/heartbeat-runs"):
            return {"runs": [{"id": "run-1", "issueId": "issue-1", "status": "succeeded", "transcript": "private"}]}
        raise AssertionError(path)

    monkeypatch.setattr(public_board, "_fetch_json", fake_fetch)

    resp = client.get(f"{PREFIX}/board")
    assert resp.status_code == 200
    assert client.post(f"{PREFIX}/board").status_code == 405
    body = resp.json()
    assert body["companyId"] == "company-1"
    assert body["disclosureBoundary"]["excluded"] == [
        "comments",
        "private documents",
        "attachments",
        "secrets",
        "customer data",
        "write endpoints",
    ]

    raw = resp.text
    for forbidden in ("private task body", "adapterConfig", "secret", "prompt", "transcript", "isol8-prod.local"):
        assert forbidden not in raw

    issue = body["issues"][0]
    assert set(issue) == {"id", "identifier", "title", "status", "priority", "assigneeAgentId", "responsibleUserId", "workProducts"}
    assert issue["workProducts"] == [{"id": "wp-1", "title": "PR", "url": "https://github.com/acme/repo/pull/1"}, {"id": "wp-2", "title": "internal"}]
    assert body["routines"] == [{"id": "routine-1", "title": "Friday review", "reviewerAgentId": "agent-2"}]
    assert body["runs"] == [{"id": "run-1", "issueId": "issue-1", "status": "succeeded"}]
    assert calls[0] == (
        "/api/companies/company-1/issues",
        {"status": "todo,in_progress,in_review,blocked,done", "limit": "100"},
    )
