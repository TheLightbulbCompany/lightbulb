"""Tests for the paperclip.upstream.error{kind} metric emission (PRIMARY emit).

When the proxy's upstream httpx request raises an ``httpx.HTTPError`` (e.g. a
connect/transport error because the Paperclip BFF is down), the proxy returns a
502 AND emits ``paperclip.upstream.error{kind:"unreachable"}`` (best-effort) so
the W63 paperclip-down alarm can fire. The metric must never mask the 502.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.requests import Request

from core.auth import AuthContext
from routers import paperclip_proxy


def _make_request(method: str = "GET", path: str = "agents") -> Request:
    """Build a minimal ASGI Request the proxy handler can consume."""
    scope = {
        "type": "http",
        "method": method,
        "path": f"/{path}",
        "raw_path": f"/{path}".encode(),
        "query_string": b"",
        "headers": [(b"host", b"company.isol8.co")],
        "client": ("203.0.113.1", 12345),
        "scheme": "https",
        "server": ("company.isol8.co", 443),
    }

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive=_receive)


class _RaisingClient:
    """Async-context-manager httpx client whose ``request`` always raises a
    transport error — mimics the Paperclip BFF being unreachable."""

    async def __aenter__(self) -> "_RaisingClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise httpx.ConnectError("paperclip down")


@pytest.mark.asyncio
async def test_proxy_upstream_unreachable_emits_metric():
    auth = AuthContext(user_id="human_x", org_id="user_x", org_role="org:admin", email="u@isol8.co")
    # org_id is a required field on PaperclipCompany (paperclip_repo.py) and the
    # proxy reads it to gate the established-session forward path
    # (``company.org_id != resolve_owner_id(auth)``). For this org-less personal
    # session resolve_owner_id(auth) == auth.user_id == "user_x", so pin org_id
    # to "user_x" — otherwise the proxy takes the provisioning-stub / re-handshake
    # branch instead of forwarding to the (raising) upstream client.
    company = SimpleNamespace(status="active", org_id="user_x", paperclip_password_encrypted="enc")

    with (
        patch("routers.paperclip_proxy.put_metric") as mock_put,
        patch("routers.paperclip_proxy._circuit_open", return_value=False),
        patch(
            "routers.paperclip_proxy._get_paperclip_user",
            new=AsyncMock(return_value=auth),
        ),
        # Established-session path: active company row + a trusted reused
        # cookie so the proxy forwards immediately (skips _sign_in) and the
        # raising client trips the except httpx.HTTPError handler.
        patch(
            "routers.paperclip_proxy.PaperclipRepo",
            return_value=SimpleNamespace(get=AsyncMock(return_value=company)),
        ),
        patch("routers.paperclip_proxy.is_trusted", return_value="reused-cookie"),
        patch("routers.paperclip_proxy.httpx.AsyncClient", return_value=_RaisingClient()),
    ):
        resp = await paperclip_proxy.proxy(path="agents", request=_make_request())

    # The 502 is preserved — the metric never masks the upstream error.
    assert resp.status_code == 502
    kinds = [
        c.kwargs.get("dimensions", {}).get("kind")
        for c in mock_put.call_args_list
        if c.args and c.args[0] == "paperclip.upstream.error"
    ]
    assert kinds == ["unreachable"]
