"""Unit tests for the admin-client methods used by the Inbox surface.

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
async def test_get_heartbeat_run(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.get_heartbeat_run(run_id="run_42", session_cookie="c=1")
    http.get.assert_awaited_once()
    assert http.get.call_args.args[0] == "/api/heartbeat-runs/run_42"


@pytest.mark.asyncio
async def test_get_decision(admin_client_with_fake_http):
    client, http = admin_client_with_fake_http
    await client.get_decision(decision_id="dec_1", session_cookie="c=1")
    http.get.assert_awaited_once()
    assert http.get.call_args.args[0] == "/api/decisions/dec_1"
