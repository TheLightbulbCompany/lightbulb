"""Unit tests for the issue-comments admin-client methods (PR #3d Task 1).

Verify ``list_issue_comments`` and ``add_issue_comment`` call the right
upstream URL with the right HTTP verb, forward the session cookie via
headers, pass the JSON body through unmodified, and return the parsed
JSON response from the upstream Paperclip server.

The fake-httpx fixture mirrors ``test_paperclip_admin_client_inbox.py``
so the assertion style is consistent with the rest of the suite.
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
async def test_list_issue_comments_hits_correct_url(admin_client_with_fake_http):
    """GET /api/issues/{id}/comments — URL + session cookie forwarded."""
    client, http = admin_client_with_fake_http
    await client.list_issue_comments(issue_id="iss_42", session_cookie="pc=abc")
    http.get.assert_awaited_once()
    call = http.get.call_args
    assert call.args[0] == "/api/issues/iss_42/comments"
    # Session cookie is threaded through headers by the _get helper.
    headers = call.kwargs.get("headers", {})
    assert headers.get("Cookie") == "pc=abc"


@pytest.mark.asyncio
async def test_list_issue_comments_returns_parsed_response(
    admin_client_with_fake_http,
):
    """Parsed JSON response from upstream is returned verbatim."""
    client, http = admin_client_with_fake_http
    fake_payload = {
        "comments": [
            {
                "id": "c1",
                "body": "Hello",
                "createdAt": "2026-05-05T00:00:00Z",
            }
        ]
    }
    http.get.return_value.json.return_value = fake_payload
    http.get.return_value.content = b'{"comments":[]}'  # non-empty
    result = await client.list_issue_comments(issue_id="iss_1", session_cookie="pc=cookie")
    assert result == fake_payload


@pytest.mark.asyncio
async def test_add_issue_comment_posts_body(admin_client_with_fake_http):
    """POST /api/issues/{id}/comments — URL, body, and cookie forwarded."""
    client, http = admin_client_with_fake_http
    await client.add_issue_comment(
        issue_id="iss_42",
        body={"body": "Looks good"},
        session_cookie="pc=abc",
    )
    http.post.assert_awaited_once()
    call = http.post.call_args
    assert call.args[0] == "/api/issues/iss_42/comments"
    assert call.kwargs.get("json") == {"body": "Looks good"}
    headers = call.kwargs.get("headers", {})
    assert headers.get("Cookie") == "pc=abc"


@pytest.mark.asyncio
async def test_add_issue_comment_returns_created_comment(
    admin_client_with_fake_http,
):
    """The created-comment dict from upstream is returned verbatim."""
    client, http = admin_client_with_fake_http
    created = {
        "id": "c2",
        "body": "World",
        "createdAt": "2026-05-05T00:01:00Z",
        "authorUserId": "u_1",
    }
    http.post.return_value.json.return_value = created
    http.post.return_value.content = b'{"id":"c2"}'
    result = await client.add_issue_comment(
        issue_id="iss_1",
        body={"body": "World"},
        session_cookie="pc=cookie",
    )
    assert result == created
