from unittest.mock import AsyncMock, MagicMock, call

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle


def _wire_prepare(monkeypatch, agents):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.list_routines = AsyncMock(return_value=[])
    admin.list_issues = AsyncMock(return_value=[])
    admin.patch_routine = AsyncMock(return_value={})
    admin.patch_issue = AsyncMock(return_value={})
    http = MagicMock()
    http.aclose = AsyncMock()
    monkeypatch.setattr(lifecycle, "PaperclipRepo", lambda table_name: MagicMock())
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="company"))
    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: http)
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_seeded_agent_ids",
        AsyncMock(side_effect=AssertionError("lifecycle must not depend on seeded ids")),
    )
    return admin


@pytest.mark.asyncio
async def test_specialist_delete_uses_surviving_root_ceo(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "root", "name": "Arbitrary", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Friday", "role": "researcher", "reportsTo": "root"},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="specialist")

    assert result["replacement_agent_id"] == "root"


@pytest.mark.asyncio
async def test_renamed_final_root_ceo_cannot_be_deleted(monkeypatch):
    admin = _wire_prepare(
        monkeypatch,
        [
            {"id": "root", "name": "Ada", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Friday", "role": "researcher", "reportsTo": "root"},
        ],
    )

    with pytest.raises(RuntimeError, match="final root CEO"):
        await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="root")

    admin.list_routines.assert_not_awaited()
    admin.list_issues.assert_not_awaited()


@pytest.mark.asyncio
async def test_root_ceo_can_be_deleted_after_another_root_exists(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "old-root", "name": "Ada", "role": "ceo", "reportsTo": None},
            {"id": "new-root", "name": "Bea", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Cid", "role": "engineer", "reportsTo": "old-root"},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="old-root")

    assert result["replacement_agent_id"] == "new-root"


@pytest.mark.asyncio
async def test_replacement_falls_back_to_first_surviving_agent(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "retiring", "name": "Ada", "role": "general", "reportsTo": None},
            {"id": "first", "name": "Bea", "role": "engineer", "reportsTo": None},
            {"id": "second", "name": "Cid", "role": "researcher", "reportsTo": None},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="retiring")

    assert result["replacement_agent_id"] == "first"


@pytest.mark.asyncio
async def test_paperclip_delete_conditionally_removes_starter_uuid(monkeypatch):
    repo = MagicMock()
    repo.remove_starter_agent_id = AsyncMock(return_value=None)
    monkeypatch.setattr(lifecycle, "PaperclipRepo", lambda table_name: repo)
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="company"))
    admin = MagicMock()
    admin.delete_agent = AsyncMock(return_value={})
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    http = MagicMock()
    http.aclose = AsyncMock()
    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: http)

    await lifecycle.delete_paperclip_for_chat(owner_id="owner", agent_id="starter-uuid")
    await lifecycle.delete_paperclip_for_chat(owner_id="owner", agent_id="starter-uuid")

    assert admin.delete_agent.await_count == 2
    assert repo.remove_starter_agent_id.await_args_list == [
        call("owner", "starter-uuid"),
        call("owner", "starter-uuid"),
    ]
