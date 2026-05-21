"""Unit tests for the new admin-client methods used by Inbox deep port (PR #3a).

Verify each method calls the right upstream URL with the right HTTP verb,
forwards the session cookie via headers, and returns the parsed JSON body.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def admin_client_with_fake_http():
    """Build a PaperclipAdminClient with a fake httpx-like http object."""
    from core.services.paperclip_admin_client import PaperclipAdminClient

    fake_http = MagicMock()
    # Default: succeed with empty JSON body. Tests override per-call.
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.json.return_value = {}
    resp.text = "{}"
    fake_http.get = AsyncMock(return_value=resp)
    fake_http.post = AsyncMock(return_value=resp)
    fake_http.delete = AsyncMock(return_value=resp)
    fake_http.patch = AsyncMock(return_value=resp)

    client = PaperclipAdminClient(http_client=fake_http)
    return client, fake_http


@pytest.mark.asyncio
async def test_list_company_heartbeat_runs(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.list_company_heartbeat_runs(company_id="co_x", session_cookie="c=1", status="failed")
    http.get.assert_awaited_once()
    call = http.get.call_args
    assert call.args[0] == "/api/companies/co_x/heartbeat-runs"
    assert call.kwargs.get("params", {}).get("status") == "failed"


@pytest.mark.asyncio
async def test_list_company_live_runs(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.list_company_live_runs(company_id="co_x", session_cookie="c=1")
    http.get.assert_awaited_once()
    assert http.get.call_args.args[0] == "/api/companies/co_x/live-runs"


@pytest.mark.asyncio
async def test_get_heartbeat_run(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.get_heartbeat_run(run_id="run_42", session_cookie="c=1")
    http.get.assert_awaited_once()
    assert http.get.call_args.args[0] == "/api/heartbeat-runs/run_42"


@pytest.mark.asyncio
async def test_archive_issue(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.archive_issue(issue_id="iss_7", session_cookie="c=1")
    http.post.assert_awaited_once()
    call = http.post.call_args
    assert call.args[0] == "/api/issues/iss_7/inbox-archive"


@pytest.mark.asyncio
async def test_unarchive_issue(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.unarchive_issue(issue_id="iss_7", session_cookie="c=1")
    http.delete.assert_awaited_once()
    assert http.delete.call_args.args[0] == "/api/issues/iss_7/inbox-archive"


@pytest.mark.asyncio
async def test_mark_issue_read(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.mark_issue_read(issue_id="iss_7", session_cookie="c=1")
    http.post.assert_awaited_once()
    assert http.post.call_args.args[0] == "/api/issues/iss_7/read"


@pytest.mark.asyncio
async def test_mark_issue_unread(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.mark_issue_unread(issue_id="iss_7", session_cookie="c=1")
    http.delete.assert_awaited_once()
    assert http.delete.call_args.args[0] == "/api/issues/iss_7/read"


@pytest.mark.asyncio
async def test_get_approval_detail(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.get_approval(approval_id="apv_1", session_cookie="c=1")
    http.get.assert_awaited_once()
    assert http.get.call_args.args[0] == "/api/approvals/apv_1"


@pytest.mark.asyncio
async def test_list_inbox_with_filters(admin_client_with_fake_http):
    """The expanded inbox listing accepts filter params and forwards them
    on the inbox-lite endpoint."""
    client, http = admin_client_with_fake_http
    await client.list_inbox_for_session_user(
        session_cookie="c=1",
        params={"tab": "mine", "status": "todo", "search": "fix"},
    )
    http.get.assert_awaited_once()
    call = http.get.call_args
    assert call.args[0] == "/api/agents/me/inbox-lite"
    assert call.kwargs.get("params") == {
        "tab": "mine",
        "status": "todo",
        "search": "fix",
    }
