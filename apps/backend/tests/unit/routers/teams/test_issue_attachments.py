# apps/backend/tests/unit/routers/teams/test_issue_attachments.py
"""Tests for the Teams issue-attachments BFF proxy (list/upload/download/delete).

The load-bearing assertions:
  - upload forwards ``ctx.company_id`` (server-derived), NEVER a client param,
  - upload rejects an oversized file with 413 before any upstream call,
  - download streams upstream bytes through with the upstream Content-Type,
  - delete maps to the attachment-scoped upstream route.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from main import app
from routers.teams import issues as issues_mod
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx() -> TeamsContext:
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


def _patch_admin(monkeypatch, admin: MagicMock) -> None:
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def test_list_attachments_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_attachments = AsyncMock(
        return_value=[
            {"id": "att1", "originalFilename": "a.png", "contentPath": "/api/attachments/att1/content"},
            {"id": "att2", "originalFilename": "b.pdf"},
        ]
    )
    _patch_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/issues/iss1/attachments", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {
        "attachments": [
            {"id": "att1", "originalFilename": "a.png", "contentPath": "/api/attachments/att1/content"},
            {"id": "att2", "originalFilename": "b.pdf"},
        ]
    }
    admin.list_issue_attachments.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")


# --------------------------------------------------------------------------
# upload — the IDOR-sensitive route
# --------------------------------------------------------------------------


def test_upload_uses_ctx_company_id_not_a_client_param(client, monkeypatch):
    """The company segment forwarded upstream MUST be ``ctx.company_id``
    (server-derived), regardless of anything the client supplies."""
    admin = MagicMock()
    admin.upload_issue_attachment = AsyncMock(return_value={"id": "att_new", "byteSize": 5})
    _patch_admin(monkeypatch, admin)

    # A malicious extra ``company_id`` form/query field must be ignored: the
    # handler only ever reads ``ctx.company_id``.
    r = client.post(
        "/api/v1/teams/issues/iss1/attachments?company_id=co_EVIL",
        files={"file": ("hello.txt", b"hello", "text/plain")},
        data={"company_id": "co_EVIL"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"id": "att_new", "byteSize": 5}

    admin.upload_issue_attachment.assert_awaited_once()
    kwargs = admin.upload_issue_attachment.await_args.kwargs
    assert kwargs["company_id"] == "co_abc"  # server-derived, NOT co_EVIL
    assert kwargs["issue_id"] == "iss1"
    assert kwargs["session_cookie"] == "cookie"
    assert kwargs["filename"] == "hello.txt"
    assert kwargs["content_type"] == "text/plain"
    assert kwargs["body"] == b"hello"


def test_upload_rejects_oversized_file_with_413(client, monkeypatch):
    admin = MagicMock()
    admin.upload_issue_attachment = AsyncMock(return_value={})
    _patch_admin(monkeypatch, admin)

    # One byte over the BFF cap.
    oversized = b"x" * (issues_mod._ATTACHMENT_MAX_BYTES + 1)
    r = client.post(
        "/api/v1/teams/issues/iss1/attachments",
        files={"file": ("big.bin", oversized, "application/octet-stream")},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 413
    # The size cap is enforced BEFORE any upstream call.
    admin.upload_issue_attachment.assert_not_awaited()


def test_upload_rejects_by_content_length_before_parsing(client, monkeypatch):
    """An oversized Content-Length is rejected 413 BEFORE the multipart body is
    parsed/spooled — so a huge upload never buffers. We send a small body but
    spoof a large Content-Length to prove the fence fires on the header alone."""
    admin = MagicMock()
    admin.upload_issue_attachment = AsyncMock(return_value={})
    _patch_admin(monkeypatch, admin)

    huge = str(issues_mod._ATTACHMENT_REQUEST_MAX_BYTES + 1)
    r = client.post(
        "/api/v1/teams/issues/iss1/attachments",
        content=b"x",  # tiny actual body
        headers={
            "Authorization": "Bearer x",
            "Content-Type": "multipart/form-data; boundary=z",
            "Content-Length": huge,
        },
    )
    assert r.status_code == 413
    admin.upload_issue_attachment.assert_not_awaited()


def test_upload_passes_octet_stream_when_content_type_missing(client, monkeypatch):
    """A file part with no content-type falls back to application/octet-stream."""
    admin = MagicMock()
    admin.upload_issue_attachment = AsyncMock(return_value={"id": "att_new"})
    _patch_admin(monkeypatch, admin)

    # Sending a part with an explicitly empty content type — Starlette surfaces
    # this as ``content_type=None`` on the UploadFile.
    r = client.post(
        "/api/v1/teams/issues/iss1/attachments",
        files={"file": ("noctype.dat", b"data", None)},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    kwargs = admin.upload_issue_attachment.await_args.kwargs
    assert kwargs["content_type"] == "application/octet-stream"


# --------------------------------------------------------------------------
# download / content stream
# --------------------------------------------------------------------------


def test_get_content_streams_bytes_with_upstream_content_type(client, monkeypatch):
    admin = MagicMock()

    # A fake upstream httpx.Response that streams two chunks and carries a
    # Content-Type + Content-Disposition the BFF must pass through.
    class _FakeUpstream:
        def __init__(self):
            self.headers = httpx.Headers(
                {
                    "content-type": "image/png",
                    "content-disposition": 'inline; filename="a.png"',
                    "x-content-type-options": "nosniff",
                }
            )
            self.closed = False

        async def aiter_bytes(self):
            yield b"PNG-"
            yield b"BYTES"

        async def aclose(self):
            self.closed = True

    fake = _FakeUpstream()
    admin.open_attachment_stream = AsyncMock(return_value=fake)
    _patch_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/attachments/att1/content", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.content == b"PNG-BYTES"
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == 'inline; filename="a.png"'
    assert r.headers["x-content-type-options"] == "nosniff"
    admin.open_attachment_stream.assert_awaited_once_with(session_cookie="cookie", attachment_id="att1")
    assert fake.closed is True  # connection released after the pump


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def test_delete_attachment_maps_to_attachment_route(client, monkeypatch):
    admin = MagicMock()
    admin.delete_attachment = AsyncMock(return_value={"ok": True})
    _patch_admin(monkeypatch, admin)

    r = client.delete(
        "/api/v1/teams/issues/iss1/attachments/att1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    admin.delete_attachment.assert_awaited_once_with(session_cookie="cookie", attachment_id="att1")
