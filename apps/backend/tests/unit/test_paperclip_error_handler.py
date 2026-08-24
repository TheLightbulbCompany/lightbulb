"""The PaperclipApiError handler must not leak Paperclip internals on 5xx.

PaperclipApiError's message is "GET /api/companies/<company_uuid>/... -> <code>",
so echoing str(exc) to the client on an upstream 5xx disclosed the internal path
+ company UUID. The handler now returns a generic body for 5xx (and logs the real
detail server-side) while leaving client-meaningful 4xx errors intact.
"""

import json

import pytest

from core.services.paperclip_admin_client import PaperclipApiError
from main import paperclip_api_error_handler


class _State:
    """Bare state with no teams_user_id, so the 401 self-heal branch is skipped."""


class _Req:
    def __init__(self):
        self.state = _State()
        self.cookies = {}


@pytest.mark.asyncio
async def test_5xx_returns_generic_body_without_path_or_uuid():
    leaky = "GET /api/companies/11111111-2222-3333-4444-555555555555/issues -> 500"
    resp = await paperclip_api_error_handler(_Req(), PaperclipApiError(leaky, status_code=500, body="boom"))
    assert resp.status_code == 502
    assert json.loads(resp.body) == {"detail": "upstream error", "upstream_status": 502}
    # The internal path + company UUID must not appear anywhere in the response.
    assert b"companies" not in resp.body
    assert b"11111111" not in resp.body


@pytest.mark.asyncio
async def test_503_also_generic():
    resp = await paperclip_api_error_handler(
        _Req(), PaperclipApiError("GET /api/companies/abc/issues -> 503", status_code=503, body="")
    )
    assert resp.status_code == 502
    assert json.loads(resp.body)["detail"] == "upstream error"


@pytest.mark.asyncio
async def test_4xx_detail_preserved():
    """Client-meaningful 4xx errors are unchanged (status + detail surfaced)."""
    resp = await paperclip_api_error_handler(_Req(), PaperclipApiError("GET /thing -> 404", status_code=404, body=""))
    assert resp.status_code == 404
    assert json.loads(resp.body)["upstream_status"] == 404


@pytest.mark.asyncio
async def test_4xx_detail_excludes_upstream_body():
    """str(exc) carries the upstream body for logs; the client must not see it."""
    exc = PaperclipApiError(
        "POST /api/companies/abc/agents -> 400",
        status_code=400,
        body='{"error":"Validation error","details":[{"path":["role"]}]}',
    )
    # The body is on str(exc) so logger.exception/traceback records the reason...
    assert "Validation error" in str(exc)
    # ...but never reaches the browser.
    resp = await paperclip_api_error_handler(_Req(), exc)
    assert resp.status_code == 400
    assert json.loads(resp.body)["detail"] == "POST /api/companies/abc/agents -> 400"
    assert b"Validation error" not in resp.body


def test_str_truncates_a_large_body():
    exc = PaperclipApiError("POST /x -> 400", status_code=400, body="z" * 5000)
    rendered = str(exc)
    assert rendered.endswith("...[truncated]")
    assert len(rendered) < 1000


def test_str_is_bare_message_when_body_is_empty():
    assert str(PaperclipApiError("GET /x -> 404", status_code=404, body="")) == "GET /x -> 404"
