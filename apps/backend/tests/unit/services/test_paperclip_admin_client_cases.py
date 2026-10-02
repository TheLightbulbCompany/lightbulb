"""Unit tests for the Cases admin-client methods.

Each is a thin proxy; what matters is the upstream path, the verb, and that the
CALLER's session cookie rides along. Paths read off the fork's
``server/src/routes/cases.ts`` @ v2026.916.1-isol8.3. Mirrors the fake-httpx
style of ``test_paperclip_admin_client_attachments.py``.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

_COOKIE = {"Cookie": "pc=abc"}


@pytest.fixture
def admin_client_with_fake_http():
    from core.services.paperclip_admin_client import PaperclipAdminClient

    fake_http = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.json.return_value = {}
    resp.text = "{}"
    fake_http.get = AsyncMock(return_value=resp)
    fake_http.post = AsyncMock(return_value=resp)
    fake_http.patch = AsyncMock(return_value=resp)
    return PaperclipAdminClient(http_client=fake_http), fake_http


def _cookie(call) -> str | None:
    return call.kwargs.get("headers", {}).get("Cookie")


@pytest.mark.asyncio
async def test_list_cases_is_company_scoped_and_forwards_params(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    params = {"types": ["blog_post", "tweet"], "status": "active", "limit": 200}
    await client.list_cases(company_id="co_abc", session_cookie="pc=abc", params=params)
    call = http.get.call_args
    assert call.args[0] == "/api/companies/co_abc/cases"
    assert call.kwargs["params"] == params
    assert _cookie(call) == _COOKIE["Cookie"]


@pytest.mark.asyncio
async def test_case_reads_hit_their_paths(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http

    await client.get_case(case_id="LB-C7", session_cookie="pc=abc")
    assert http.get.call_args.args[0] == "/api/cases/LB-C7"
    assert _cookie(http.get.call_args) == "pc=abc"

    await client.list_case_events(case_id="c1", limit=50, session_cookie="pc=abc")
    assert http.get.call_args.args[0] == "/api/cases/c1/events"
    assert http.get.call_args.kwargs["params"] == {"limit": 50}

    await client.list_issue_cases(issue_id="iss1", session_cookie="pc=abc")
    assert http.get.call_args.args[0] == "/api/issues/iss1/cases"

    await client.list_case_document_revisions(case_id="c1", key="draft", session_cookie="pc=abc")
    assert http.get.call_args.args[0] == "/api/cases/c1/documents/draft/revisions"

    await client.list_case_document_annotations(case_id="c1", key="draft", status="all", session_cookie="pc=abc")
    assert http.get.call_args.args[0] == "/api/cases/c1/documents/draft/annotations"
    assert http.get.call_args.kwargs["params"] == {"status": "all", "includeComments": "true"}


@pytest.mark.asyncio
async def test_case_writes_hit_their_paths(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http

    await client.patch_case(case_id="c1", body={"status": "done"}, session_cookie="pc=abc")
    assert http.patch.call_args.args[0] == "/api/cases/c1"
    assert http.patch.call_args.kwargs["json"] == {"status": "done"}
    assert _cookie(http.patch.call_args) == "pc=abc"

    await client.restore_case_document_revision(case_id="c1", key="draft", revision_id="r1", session_cookie="pc=abc")
    assert http.post.call_args.args[0] == "/api/cases/c1/documents/draft/revisions/r1/restore"

    await client.create_case_document_annotation(case_id="c1", key="draft", body={"body": "x"}, session_cookie="pc=abc")
    assert http.post.call_args.args[0] == "/api/cases/c1/documents/draft/annotations"

    await client.add_case_document_annotation_comment(
        case_id="c1", key="draft", thread_id="t1", body={"body": "x"}, session_cookie="pc=abc"
    )
    assert http.post.call_args.args[0] == "/api/cases/c1/documents/draft/annotations/t1/comments"

    await client.update_case_document_annotation_thread(
        case_id="c1", key="draft", thread_id="t1", body={"status": "resolved"}, session_cookie="pc=abc"
    )
    assert http.patch.call_args.args[0] == "/api/cases/c1/documents/draft/annotations/t1"


@pytest.mark.asyncio
async def test_open_asset_stream_raises_and_closes_on_error(admin_client_with_fake_http):
    from core.services.paperclip_admin_client import PaperclipApiError

    client, http = admin_client_with_fake_http
    resp = MagicMock()
    resp.status_code = 404
    resp.text = "{}"
    resp.aread = AsyncMock()
    resp.aclose = AsyncMock()
    http.build_request = MagicMock(return_value="req")
    http.send = AsyncMock(return_value=resp)

    with pytest.raises(PaperclipApiError):
        await client.open_asset_stream(asset_id="asset9", session_cookie="pc=abc")
    assert http.build_request.call_args.args[:2] == ("GET", "/api/assets/asset9/content")
    http.send.assert_awaited_once_with("req", stream=True)
    resp.aclose.assert_awaited_once()
