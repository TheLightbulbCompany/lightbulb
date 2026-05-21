"""Tests for PaperclipAdminClient.update_agent — partial-update wrapper
around Paperclip's PATCH /api/companies/{companyId}/agents/{agentId}."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError  # noqa: E402


def _stub_http_client(response: httpx.Response) -> MagicMock:
    """Build a stub httpx.AsyncClient whose .patch() / .post() returns the
    given response. Returns the mock so tests can introspect calls."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_update_agent_sends_only_provided_fields():
    """A partial update with just ``title`` set must send a PATCH body
    that ONLY contains ``title`` — empty values must not be sent as
    null/empty strings (would wipe other fields server-side)."""
    response = httpx.Response(200, json={"id": "agent-uuid", "title": "New title"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    result = await admin.update_agent(
        session_cookie="sess=abc",
        company_id="co-1",
        agent_id="agent-uuid",
        title="New title",
    )

    assert result == {"id": "agent-uuid", "title": "New title"}
    http.patch.assert_awaited_once()
    call = http.patch.await_args
    # URL — relative path per the admin client's base_url contract.
    assert call.args[0] == "/api/companies/co-1/agents/agent-uuid"
    # Body — only title.
    assert call.kwargs["json"] == {"title": "New title"}
    # Session cookie threaded through headers.
    assert "sess=abc" in call.kwargs["headers"]["Cookie"]


@pytest.mark.asyncio
async def test_update_agent_sends_all_provided_fields():
    """Multiple fields in one call get bundled into the same PATCH body."""
    response = httpx.Response(200, json={"id": "a"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    await admin.update_agent(
        session_cookie="c",
        company_id="co",
        agent_id="a",
        role="engineer",
        title="Backend dev",
        capabilities="Writes Python.",
        icon="terminal",
        metadata={"source": "marketplace"},
    )

    body = http.patch.await_args.kwargs["json"]
    assert body["role"] == "engineer"
    assert body["title"] == "Backend dev"
    assert body["capabilities"] == "Writes Python."
    assert body["icon"] == "terminal"
    assert body["metadata"] == {"source": "marketplace"}


@pytest.mark.asyncio
async def test_update_agent_skips_none_fields():
    """``None`` means "don't touch" — must NOT appear in the body."""
    response = httpx.Response(200, json={"id": "a"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    await admin.update_agent(
        session_cookie="c",
        company_id="co",
        agent_id="a",
        role=None,
        title="New title",
        capabilities=None,
        icon=None,
    )

    body = http.patch.await_args.kwargs["json"]
    assert body == {"title": "New title"}
    assert "role" not in body
    assert "capabilities" not in body


@pytest.mark.asyncio
async def test_update_agent_raises_paperclip_api_error_on_non_2xx():
    """Server-side validation failures surface as PaperclipApiError, so
    callers (e.g. the chat interceptor) can decide whether to swallow
    or propagate."""
    response = httpx.Response(422, json={"error": "invalid role"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    with pytest.raises(PaperclipApiError):
        await admin.update_agent(
            session_cookie="c",
            company_id="co",
            agent_id="a",
            role="bogus",
        )


@pytest.mark.asyncio
async def test_update_agent_returns_dict_payload():
    """200 path returns the server's JSON dict so callers can read the
    updated row without re-fetching."""
    response = httpx.Response(200, json={"id": "a", "title": "T", "updatedAt": "2026-01-01"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    result = await admin.update_agent(
        session_cookie="c",
        company_id="co",
        agent_id="a",
        title="T",
    )

    assert result["id"] == "a"
    assert result["title"] == "T"
    assert result["updatedAt"] == "2026-01-01"


@pytest.mark.asyncio
async def test_create_agent_sends_title_capabilities_icon_metadata_when_provided():
    """Regression-guard: create_agent's signature has accepted
    title/capabilities for a while but historically the request body
    silently dropped them. Confirm they're sent through to Paperclip."""
    response = httpx.Response(200, json={"id": "new-agent"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    await admin.create_agent(
        session_cookie="c",
        company_id="co",
        name="Ray",
        role="general",
        adapter_type="openclaw_gateway",
        adapter_config={"url": "wss://..."},
        title="Restaurant booking specialist",
        capabilities="Books Resy reservations.",
    )

    body = http.post.await_args.kwargs["json"]
    assert body["title"] == "Restaurant booking specialist"
    assert body["capabilities"] == "Books Resy reservations."
