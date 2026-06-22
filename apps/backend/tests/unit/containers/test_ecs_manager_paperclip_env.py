"""_paperclip_runtime_env resolves Friday's Paperclip auth as task env entries.

The bundled `paperclip` skill issues raw REST calls (curl) that read
PAPERCLIP_* from the agent's shell env; provision injects them onto the ECS
task env (which the agent's exec inherits) via this helper. It is best-effort:
any resolution failure must yield [] so provisioning never blocks on Paperclip.
"""

import contextlib
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import settings
from core.containers.ecs_manager import EcsManager


@contextlib.contextmanager
def _patches(
    *, agents=None, api_key="pcp_key", company_id="comp_1", url="http://pc.local:3100", fetch_side_effect=None
):
    http = MagicMock()
    http.aclose = AsyncMock()
    fetch_mock = (
        AsyncMock(side_effect=fetch_side_effect)
        if fetch_side_effect is not None
        else AsyncMock(return_value=agents or [])
    )
    with (
        patch.object(settings, "PAPERCLIP_INTERNAL_URL", url),
        patch("httpx.AsyncClient", return_value=http),
        patch("core.repositories.paperclip_repo.PaperclipRepo"),
        patch("core.services.paperclip_admin_client.PaperclipAdminClient"),
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            new=fetch_mock,
        ),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            new=AsyncMock(return_value=company_id),
        ),
        patch(
            "core.services.paperclip_api_key.ensure_paperclip_api_key",
            new=AsyncMock(return_value=api_key),
        ),
    ):
        yield http


@pytest.mark.asyncio
async def test_resolves_full_paperclip_env():
    mgr = EcsManager.__new__(EcsManager)
    with _patches(agents=[{"id": "fri", "role": "ceo"}, {"id": "a2", "role": "worker"}]) as http:
        env = await mgr._paperclip_runtime_env("org_x")
    by_name = {e["name"]: e["value"] for e in env}
    assert by_name == {
        "PAPERCLIP_API_URL": "http://pc.local:3100",
        "PAPERCLIP_API_KEY": "pcp_key",
        "PAPERCLIP_COMPANY_ID": "comp_1",
        "PAPERCLIP_AGENT_ID": "fri",
    }
    # The httpx client is always closed.
    http.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_when_no_ceo_agent():
    mgr = EcsManager.__new__(EcsManager)
    with _patches(agents=[{"id": "a", "role": "worker"}]):
        assert await mgr._paperclip_runtime_env("org_x") == []


@pytest.mark.asyncio
async def test_empty_when_no_internal_url():
    mgr = EcsManager.__new__(EcsManager)
    with _patches(agents=[{"id": "fri", "role": "ceo"}], url=""):
        assert await mgr._paperclip_runtime_env("org_x") == []


@pytest.mark.asyncio
async def test_empty_when_key_missing():
    mgr = EcsManager.__new__(EcsManager)
    with _patches(agents=[{"id": "fri", "role": "ceo"}], api_key=None):
        assert await mgr._paperclip_runtime_env("org_x") == []


@pytest.mark.asyncio
async def test_best_effort_empty_on_resolution_exception():
    mgr = EcsManager.__new__(EcsManager)
    with _patches(fetch_side_effect=RuntimeError("paperclip unreachable")) as http:
        assert await mgr._paperclip_runtime_env("org_x") == []
    # Even on failure the client is closed (finally).
    http.aclose.assert_awaited_once()
