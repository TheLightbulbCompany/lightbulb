"""Tests for the paperclip.upstream.error{kind} SECONDARY emit.

The four request helpers in ``PaperclipAdminClient`` emit:
  - ``{kind:"unreachable"}`` on an httpx transport error (and re-raise), and
  - ``{kind:"5xx"}`` on a >= 500 response (alongside the PaperclipApiError).

A 4xx response (e.g. 404 on delete) must NOT emit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError


def _kinds(mock_put) -> list[str]:
    return [
        c.kwargs.get("dimensions", {}).get("kind")
        for c in mock_put.call_args_list
        if c.args and c.args[0] == "paperclip.upstream.error"
    ]


@pytest.mark.asyncio
async def test_transport_error_emits_unreachable_and_reraises():
    http = MagicMock()
    http.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    client = PaperclipAdminClient(http_client=http)
    with patch("core.services.paperclip_admin_client.put_metric") as mp:
        with pytest.raises(httpx.RequestError):
            await client._get("/api/companies", session_cookie="c")
    assert _kinds(mp) == ["unreachable"]


@pytest.mark.asyncio
async def test_5xx_response_emits_5xx_and_raises_api_error():
    resp = MagicMock(status_code=503, text="boom", content=b"boom")
    http = MagicMock()
    http.post = AsyncMock(return_value=resp)
    client = PaperclipAdminClient(http_client=http)
    with patch("core.services.paperclip_admin_client.put_metric") as mp:
        with pytest.raises(PaperclipApiError):
            await client._post("/x", json={}, session_cookie="c")
    assert _kinds(mp) == ["5xx"]


@pytest.mark.asyncio
async def test_404_delete_emits_nothing():
    # 404 on delete is treated as already-gone (idempotent) — not an upstream
    # error, so no metric.
    resp = MagicMock(status_code=404, text="", content=b"")
    http = MagicMock()
    http.delete = AsyncMock(return_value=resp)
    client = PaperclipAdminClient(http_client=http)
    with patch("core.services.paperclip_admin_client.put_metric") as mp:
        out = await client._delete("/x", session_cookie="c")
    assert out == {}
    assert _kinds(mp) == []
