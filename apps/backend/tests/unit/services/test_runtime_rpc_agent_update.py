from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import runtime_rpc
from core.services.messaging import ApiProblem
from core.services.paperclip_admin_client import PaperclipApiError


class _Http:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _wire_admin(monkeypatch, admin):
    monkeypatch.setattr(runtime_rpc.httpx, "AsyncClient", lambda **_kwargs: _Http())
    monkeypatch.setattr(runtime_rpc, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(runtime_rpc, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(runtime_rpc, "resolve_company_id_for_owner", AsyncMock(return_value="company"))


@pytest.mark.asyncio
async def test_name_only_update_is_mirrored_to_paperclip(monkeypatch):
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_admin(monkeypatch, admin)

    await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Ada"})

    admin.patch_agent.assert_awaited_once_with(
        session_cookie="cookie",
        agent_id="a1",
        body={"name": "Ada"},
    )


@pytest.mark.asyncio
async def test_name_and_existing_metadata_share_one_patch(monkeypatch):
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_admin(monkeypatch, admin)
    params = {
        "agentId": "a1",
        "name": "Ada",
        "params": {
            "paperclip": {
                "role": "researcher",
                "title": "Research lead",
                "capabilities": "Finds evidence",
                "icon": "search",
            }
        },
    }

    await runtime_rpc._mirror_agent_update("org_1", params)

    admin.patch_agent.assert_awaited_once_with(
        session_cookie="cookie",
        agent_id="a1",
        body={
            "name": "Ada",
            "role": "researcher",
            "title": "Research lead",
            "capabilities": "Finds evidence",
            "icon": "search",
        },
    )
    assert params == {"agentId": "a1", "name": "Ada"}


@pytest.mark.asyncio
async def test_a_missing_company_refuses_instead_of_answering_ok(monkeypatch):
    """No company row means the name was written nowhere; that is a lost edit, not ok."""
    admin = MagicMock()
    admin.patch_agent = AsyncMock(return_value={})
    _wire_admin(monkeypatch, admin)
    monkeypatch.setattr(runtime_rpc, "resolve_company_id_for_owner", AsyncMock(return_value=None))

    with pytest.raises(ApiProblem) as exc:
        await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Ada"})

    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_update_skips_paperclip(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(runtime_rpc.httpx, "AsyncClient", client)

    await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "model": "provider/model"})

    client.assert_not_called()


@pytest.mark.asyncio
async def test_retryable_patch_is_retried(monkeypatch):
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(
        side_effect=[
            PaperclipApiError("PATCH /api/agents/a1 -> 503", 503, {}),
            {},
        ]
    )
    _wire_admin(monkeypatch, admin)
    sleep = AsyncMock()
    monkeypatch.setattr(runtime_rpc.asyncio, "sleep", sleep)

    await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Ada"})

    assert admin.patch_agent.await_count == 2
    sleep.assert_awaited_once_with(0.2)


class _CountingName(str):
    strip_calls = 0

    def strip(self, chars=None):
        type(self).strip_calls += 1
        return super().strip(chars)


@pytest.mark.asyncio
async def test_execute_normalizes_agent_name_once_before_the_write(monkeypatch):
    """Once, at the RPC boundary -- and never on the caller's own dict. Normalising per writer
    is how a rename lands under two different names."""
    _CountingName.strip_calls = 0
    mirror = AsyncMock()
    monkeypatch.setattr(runtime_rpc, "_mirror_agent_update", mirror)
    supplied = {"agentId": "a1", "name": _CountingName("  Ada  ")}

    await runtime_rpc.execute_sync(
        owner_id="org_1",
        user_id="user_1",
        request_id="request",
        method="agents.update",
        params=supplied,
    )

    assert _CountingName.strip_calls == 1
    assert mirror.await_args.args[1]["name"] == "Ada"
    assert supplied["name"] == "  Ada  "


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   "])
async def test_execute_rejects_empty_agent_name_at_rpc_boundary(monkeypatch, name):
    mirror = AsyncMock()
    monkeypatch.setattr(runtime_rpc, "_mirror_agent_update", mirror)
    call_mock = AsyncMock(return_value={})
    monkeypatch.setattr(runtime_rpc.container_rpc, "call", call_mock)

    with pytest.raises(ApiProblem) as exc_info:
        await runtime_rpc.execute_sync(
            owner_id="org_1",
            user_id="user_1",
            request_id="request",
            method="agents.update",
            params={"agentId": "a1", "name": name},
        )

    assert exc_info.value.error.code == "INVALID_REQUEST"
    mirror.assert_not_awaited()
    call_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_root_ceo_cannot_be_demoted(monkeypatch):
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "ceo", "reportsTo": None})
    admin.list_agents = AsyncMock(return_value=[{"id": "a1", "companyId": "company", "role": "ceo", "reportsTo": None}])
    admin.patch_agent = AsyncMock(return_value={})
    _wire_admin(monkeypatch, admin)

    with pytest.raises(RuntimeError, match="coordinator cannot be deleted"):
        await runtime_rpc._mirror_agent_update(
            "org_1",
            {"agentId": "a1", "params": {"paperclip": {"role": "general"}}},
        )

    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_root_ceo_can_be_renamed(monkeypatch):
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "ceo", "reportsTo": None})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_admin(monkeypatch, admin)

    await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Renamed"})

    admin.patch_agent.assert_awaited_once()
    admin.list_agents.assert_not_called()
