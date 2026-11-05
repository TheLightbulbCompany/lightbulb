"""load_agent_roster reads the AGENT# registry for super-agent workspaces (wave1-R Task 5, review focus 2 and 3)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.config import settings
from core.repositories import agent_registry_repo as repo
from core.services import agent_registry, messaging
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.messaging import ApiProblem

WS = "org_a"
FETCH = "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner"


def _flag(on: bool):
    return patch("core.services.super_agent_flags.flags_for", AsyncMock(return_value=on))


async def _seed(roster):
    for a in roster:
        if a["status"] != "terminated":
            await repo.put_new(WS, agent_registry.row_from_paperclip(a))


@pytest.mark.asyncio
async def test_flag_off_calls_paperclip_only(paperclip_roster):
    with (
        _flag(False),
        patch(FETCH, AsyncMock(return_value=paperclip_roster)) as fetch,
        patch.object(agent_registry, "roster", AsyncMock(side_effect=AssertionError("registry read"))),
        patch("core.services.paperclip_admin_client.shared_admin_client", MagicMock()),
        patch("core.repositories.paperclip_repo.PaperclipRepo", MagicMock()),
    ):
        roster = await messaging.load_agent_roster(WS)
    fetch.assert_awaited_once()
    assert len(roster) == 7


@pytest.mark.asyncio
async def test_flag_on_reads_registry_not_paperclip(tasks_table, paperclip_roster):
    await _seed(paperclip_roster)
    with _flag(True), patch(FETCH, AsyncMock(side_effect=AssertionError("paperclip read"))):
        roster = await messaging.load_agent_roster(WS)
    assert roster == await agent_registry.roster(WS)
    assert [r["id"] for r in roster] == sorted(r["id"] for r in roster)


@pytest.mark.asyncio
async def test_registry_unavailable_maps_to_runtime_unavailable(ddb):
    with _flag(True), pytest.raises(ApiProblem) as exc:
        await messaging.load_agent_roster(WS)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc.value.error.retryable is True


@pytest.mark.asyncio
async def test_registry_bad_id_maps_to_internal_error(tasks_table):
    await repo.put_new(WS, {"agentId": "not-a-uuid", "name": "Bad"})
    with _flag(True), pytest.raises(ApiProblem) as exc:
        await messaging.load_agent_roster(WS)
    assert exc.value.error.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_create_then_roster_registry_mode(tasks_table):
    """An agent created after the backfill shows on the next roster read, no re-run."""
    new_id = "44444444-4444-4444-8444-444444444444"

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(200, json={"id": new_id, "companyId": "co-1", "status": "idle", **body})

    client = PaperclipAdminClient(
        http_client=httpx.AsyncClient(base_url="http://pc", transport=httpx.MockTransport(handler))
    )
    pc_repo = MagicMock()
    pc_repo.find_workspace_by_company = AsyncMock(return_value=MagicMock(org_id=WS))
    with (
        patch.object(settings, "SUPER_AGENT_ENV_ENABLED", True),
        patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=pc_repo),
        _flag(True),
        patch(FETCH, AsyncMock(side_effect=AssertionError("paperclip read"))),
    ):
        await client.create_agent(
            session_cookie="c",
            company_id="co-1",
            name="Ops",
            role="general",
            adapter_type="hermes_gateway",
            adapter_config={},
        )
        roster = await messaging.load_agent_roster(WS)
    assert [(r["id"], r["name"]) for r in roster] == [(new_id, "Ops")]
