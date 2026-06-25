"""Unit tests for the issue-attachment admin-client methods.

Verify the four attachment methods against the upstream contract read off
``paperclip/server/src/routes/issues.ts``:
  - ``list_issue_attachments`` → GET ``/api/issues/{id}/attachments``
  - ``upload_issue_attachment`` → POST ``/api/companies/{co}/issues/{id}/attachments``
       using the multipart ``"file"`` field, with NO JSON Content-Type so
       httpx owns the multipart boundary,
  - ``open_attachment_stream`` → streaming GET, returns the open response on
       success and raises (after closing) on a >=400,
  - ``delete_attachment`` → DELETE ``/api/attachments/{id}``.

Mirrors the fake-httpx style of ``test_paperclip_admin_client_comments.py``.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


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
    fake_http.delete = AsyncMock(return_value=resp)
    fake_http.patch = AsyncMock(return_value=resp)

    client = PaperclipAdminClient(http_client=fake_http)
    return client, fake_http


@pytest.mark.asyncio
async def test_list_issue_attachments_hits_issue_scoped_url(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    http.get.return_value.content = b"[]"
    http.get.return_value.json.return_value = [{"id": "att1"}]
    result = await client.list_issue_attachments(issue_id="iss_42", session_cookie="pc=abc")
    http.get.assert_awaited_once()
    assert http.get.call_args.args[0] == "/api/issues/iss_42/attachments"
    assert http.get.call_args.kwargs.get("headers", {}).get("Cookie") == "pc=abc"
    assert result == [{"id": "att1"}]


@pytest.mark.asyncio
async def test_upload_uses_file_field_and_company_scoped_url(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    http.post.return_value.status_code = 201
    http.post.return_value.content = b'{"id":"att_new"}'
    http.post.return_value.json.return_value = {"id": "att_new"}

    result = await client.upload_issue_attachment(
        session_cookie="pc=abc",
        company_id="co_abc",
        issue_id="iss_1",
        filename="hello.png",
        content_type="image/png",
        body=b"PNGDATA",
    )

    http.post.assert_awaited_once()
    call = http.post.call_args
    # Company-scoped upstream route (the upstream route is mounted under
    # /companies/:companyId/issues/:issueId/attachments).
    assert call.args[0] == "/api/companies/co_abc/issues/iss_1/attachments"
    # The multipart field name MUST be "file" — upstream does upload.single("file").
    files = call.kwargs.get("files")
    assert "file" in files
    fname, fbody, fctype = files["file"]
    assert fname == "hello.png"
    assert fbody == b"PNGDATA"
    assert fctype == "image/png"
    # JSON body must NOT be sent (multipart, not JSON).
    assert call.kwargs.get("json") is None
    headers = call.kwargs.get("headers", {})
    # The JSON Content-Type from _headers must be dropped so httpx can set the
    # multipart boundary itself; the auth cookie is still forwarded.
    assert "Content-Type" not in headers
    assert headers.get("Cookie") == "pc=abc"
    assert result == {"id": "att_new"}


@pytest.mark.asyncio
async def test_upload_falls_back_to_attachment_filename_when_none(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    http.post.return_value.status_code = 201
    http.post.return_value.content = b"{}"
    await client.upload_issue_attachment(
        session_cookie="pc=abc",
        company_id="co_abc",
        issue_id="iss_1",
        filename=None,
        content_type="application/octet-stream",
        body=b"data",
    )
    fname, _, _ = http.post.call_args.kwargs["files"]["file"]
    assert fname == "attachment"


@pytest.mark.asyncio
async def test_upload_raises_on_4xx(admin_client_with_fake_http):
    from core.services.paperclip_admin_client import PaperclipApiError

    client, http = admin_client_with_fake_http
    http.post.return_value.status_code = 422
    http.post.return_value.text = "too big"
    with pytest.raises(PaperclipApiError) as ei:
        await client.upload_issue_attachment(
            session_cookie="pc=abc",
            company_id="co_abc",
            issue_id="iss_1",
            filename="x",
            content_type="text/plain",
            body=b"x",
        )
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_open_attachment_stream_returns_open_response(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http

    streamed = MagicMock()
    streamed.status_code = 200
    http.build_request = MagicMock(return_value="REQ")
    http.send = AsyncMock(return_value=streamed)

    resp = await client.open_attachment_stream(session_cookie="pc=abc", attachment_id="att1")
    # The streaming GET is built for the attachment-content URL and sent with stream=True.
    http.build_request.assert_called_once()
    assert http.build_request.call_args.args[0] == "GET"
    assert http.build_request.call_args.args[1] == "/api/attachments/att1/content"
    http.send.assert_awaited_once()
    assert http.send.call_args.kwargs.get("stream") is True
    assert resp is streamed


@pytest.mark.asyncio
async def test_open_attachment_stream_closes_and_raises_on_error(admin_client_with_fake_http):
    from core.services.paperclip_admin_client import PaperclipApiError

    client, http = admin_client_with_fake_http

    streamed = MagicMock()
    streamed.status_code = 404
    streamed.text = "not found"
    streamed.aread = AsyncMock()
    streamed.aclose = AsyncMock()
    http.build_request = MagicMock(return_value="REQ")
    http.send = AsyncMock(return_value=streamed)

    with pytest.raises(PaperclipApiError) as ei:
        await client.open_attachment_stream(session_cookie="pc=abc", attachment_id="att1")
    assert ei.value.status_code == 404
    # The response is drained + closed on the error path so the socket isn't leaked.
    streamed.aread.assert_awaited_once()
    streamed.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_attachment_hits_attachment_route(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    http.delete.return_value.content = b'{"ok":true}'
    http.delete.return_value.json.return_value = {"ok": True}
    result = await client.delete_attachment(session_cookie="pc=abc", attachment_id="att1")
    http.delete.assert_awaited_once()
    assert http.delete.call_args.args[0] == "/api/attachments/att1"
    assert http.delete.call_args.kwargs.get("headers", {}).get("Cookie") == "pc=abc"
    assert result == {"ok": True}
