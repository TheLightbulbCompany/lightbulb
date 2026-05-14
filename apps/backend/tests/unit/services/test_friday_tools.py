"""Unit tests for friday_tools — backend orchestration tool service layer.

The HTTP boundary that exposes these to Friday's container is a separate
PR; these tests exercise the service functions directly with mocked
Paperclip repo + admin client.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.services import friday_tools
from core.services.friday_tools import (
    FridayToolError,
    paperclip_create_issue,
    paperclip_get_issue_status,
    paperclip_list_agents,
)
from core.services.paperclip_admin_client import PaperclipApiError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    """Mock PaperclipRepo. By default, owner_id resolves to company 'comp-1'."""
    r = MagicMock()
    r.get = AsyncMock(return_value=MagicMock(company_id="comp-1"))
    r.get_org_company_id = AsyncMock(return_value="comp-1")
    return r


@pytest.fixture
def admin_client(monkeypatch):
    """Mock PaperclipAdminClient + monkey-patch get_admin_session_cookie."""
    client = MagicMock()
    client._http = MagicMock()
    client.list_agents = AsyncMock(return_value=[])
    client.create_issue = AsyncMock(return_value={"id": "issue-1", "title": "T"})
    client.get_issue = AsyncMock(return_value={"id": "issue-1", "status": "open"})

    async def fake_cookie(_http):
        return "session=abc"

    monkeypatch.setattr(friday_tools, "get_admin_session_cookie", fake_cookie)
    return client


# ---------------------------------------------------------------------------
# paperclip_list_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_returns_ceo_first_then_alphabetical(repo, admin_client):
    admin_client.list_agents = AsyncMock(
        return_value=[
            {"id": "u-pulse", "name": "Pulse", "role": "specialist"},
            {"id": "u-friday", "name": "Friday", "role": "ceo"},
            {"id": "u-atlas", "name": "Atlas", "role": "recruiter"},
        ]
    )

    result = await paperclip_list_agents(
        owner_id="user_x", repo=repo, admin_client=admin_client
    )

    assert [a["name"] for a in result] == ["Friday", "Atlas", "Pulse"]


@pytest.mark.asyncio
async def test_list_agents_raises_when_owner_has_no_company(repo, admin_client):
    repo.get = AsyncMock(return_value=None)
    repo.get_org_company_id = AsyncMock(return_value=None)

    with pytest.raises(FridayToolError) as exc:
        await paperclip_list_agents(
            owner_id="user_x", repo=repo, admin_client=admin_client
        )

    assert exc.value.code == "owner_has_no_company"


@pytest.mark.asyncio
async def test_list_agents_org_owner_uses_org_company_id(repo, admin_client):
    """org_* owner ids route through repo.get_org_company_id (not repo.get)."""
    repo.get_org_company_id = AsyncMock(return_value="comp-org-1")
    admin_client.list_agents = AsyncMock(return_value=[])

    await paperclip_list_agents(
        owner_id="org_xyz", repo=repo, admin_client=admin_client
    )

    repo.get_org_company_id.assert_awaited_once_with("org_xyz")
    admin_client.list_agents.assert_awaited_once_with(
        session_cookie="session=abc", company_id="comp-org-1"
    )


@pytest.mark.asyncio
async def test_list_agents_translates_paperclip_outage(repo, admin_client):
    admin_client.list_agents = AsyncMock(
        side_effect=PaperclipApiError("upstream down", 503, None)
    )

    with pytest.raises(FridayToolError) as exc:
        await paperclip_list_agents(
            owner_id="user_x", repo=repo, admin_client=admin_client
        )

    assert exc.value.code == "paperclip_unavailable"


@pytest.mark.asyncio
async def test_list_agents_translates_transport_error(repo, admin_client):
    admin_client.list_agents = AsyncMock(
        side_effect=httpx.ConnectError("upstream down")
    )

    with pytest.raises(FridayToolError) as exc:
        await paperclip_list_agents(
            owner_id="user_x", repo=repo, admin_client=admin_client
        )

    assert exc.value.code == "paperclip_unavailable"


@pytest.mark.asyncio
async def test_list_agents_non_list_response_raises(repo, admin_client):
    admin_client.list_agents = AsyncMock(return_value={"unexpected": "shape"})

    with pytest.raises(FridayToolError) as exc:
        await paperclip_list_agents(
            owner_id="user_x", repo=repo, admin_client=admin_client
        )

    assert exc.value.code == "paperclip_unavailable"


# ---------------------------------------------------------------------------
# paperclip_create_issue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_happy_path(repo, admin_client):
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "a-pulse", "name": "Pulse"}]
    )

    result = await paperclip_create_issue(
        owner_id="user_x",
        title="Generate qualified leads",
        description="Find 50 SDR-fit prospects in fintech.",
        assignee_agent_id="a-pulse",
        repo=repo,
        admin_client=admin_client,
    )

    assert result["id"] == "issue-1"
    # Body uses camelCase keys (Paperclip API contract).
    call_kwargs = admin_client.create_issue.await_args.kwargs
    assert call_kwargs["company_id"] == "comp-1"
    body = call_kwargs["body"]
    assert body["title"] == "Generate qualified leads"
    assert body["description"] == "Find 50 SDR-fit prospects in fintech."
    assert body["assigneeAgentId"] == "a-pulse"
    # Optional fields omitted when None.
    assert "priority" not in body
    assert "projectId" not in body


@pytest.mark.asyncio
async def test_create_issue_includes_priority_and_project_when_set(repo, admin_client):
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "a-pulse", "name": "Pulse"}]
    )

    await paperclip_create_issue(
        owner_id="user_x",
        title="T",
        description="D",
        assignee_agent_id="a-pulse",
        repo=repo,
        admin_client=admin_client,
        priority="high",
        project_id="proj-1",
    )

    body = admin_client.create_issue.await_args.kwargs["body"]
    assert body["priority"] == "high"
    assert body["projectId"] == "proj-1"


@pytest.mark.asyncio
async def test_create_issue_rejects_assignee_not_in_roster(repo, admin_client):
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "a-pulse", "name": "Pulse"}]
    )

    with pytest.raises(FridayToolError) as exc:
        await paperclip_create_issue(
            owner_id="user_x",
            title="T",
            description="D",
            assignee_agent_id="a-someone-else",
            repo=repo,
            admin_client=admin_client,
        )

    assert exc.value.code == "assignee_not_in_roster"
    admin_client.create_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_issue_raises_when_owner_has_no_company(repo, admin_client):
    repo.get = AsyncMock(return_value=None)
    repo.get_org_company_id = AsyncMock(return_value=None)

    with pytest.raises(FridayToolError) as exc:
        await paperclip_create_issue(
            owner_id="user_x",
            title="T",
            description="D",
            assignee_agent_id="a-pulse",
            repo=repo,
            admin_client=admin_client,
        )

    assert exc.value.code == "owner_has_no_company"


@pytest.mark.asyncio
async def test_create_issue_translates_paperclip_outage_on_create(repo, admin_client):
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "a-pulse", "name": "Pulse"}]
    )
    admin_client.create_issue = AsyncMock(
        side_effect=PaperclipApiError("upstream down", 503, None)
    )

    with pytest.raises(FridayToolError) as exc:
        await paperclip_create_issue(
            owner_id="user_x",
            title="T",
            description="D",
            assignee_agent_id="a-pulse",
            repo=repo,
            admin_client=admin_client,
        )

    assert exc.value.code == "paperclip_unavailable"


# ---------------------------------------------------------------------------
# paperclip_get_issue_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_issue_status_happy_path(repo, admin_client):
    admin_client.get_issue = AsyncMock(
        return_value={
            "id": "issue-7",
            "status": "in_progress",
            "title": "Generate leads",
        }
    )

    result = await paperclip_get_issue_status(
        owner_id="user_x",
        issue_id="issue-7",
        repo=repo,
        admin_client=admin_client,
    )

    assert result["id"] == "issue-7"
    assert result["status"] == "in_progress"
    admin_client.get_issue.assert_awaited_once_with(
        session_cookie="session=abc", issue_id="issue-7"
    )


@pytest.mark.asyncio
async def test_get_issue_status_raises_when_owner_has_no_company(repo, admin_client):
    """Tenancy check: even though get_issue is keyed only on issue_id, an
    owner with no company shouldn't be issuing reads."""
    repo.get = AsyncMock(return_value=None)
    repo.get_org_company_id = AsyncMock(return_value=None)

    with pytest.raises(FridayToolError) as exc:
        await paperclip_get_issue_status(
            owner_id="user_x",
            issue_id="issue-7",
            repo=repo,
            admin_client=admin_client,
        )

    assert exc.value.code == "owner_has_no_company"


@pytest.mark.asyncio
async def test_get_issue_status_translates_paperclip_outage(repo, admin_client):
    admin_client.get_issue = AsyncMock(
        side_effect=PaperclipApiError("upstream down", 503, None)
    )

    with pytest.raises(FridayToolError) as exc:
        await paperclip_get_issue_status(
            owner_id="user_x",
            issue_id="issue-7",
            repo=repo,
            admin_client=admin_client,
        )

    assert exc.value.code == "paperclip_unavailable"
