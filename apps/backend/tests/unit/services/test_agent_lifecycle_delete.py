from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle


@pytest.mark.asyncio
async def test_prepare_delete_reassigns_open_issues_and_preserves_routines():
    admin = MagicMock()
    admin.list_agents = AsyncMock(
        return_value=[
            {"id": "friday", "role": "ceo"},
            {"id": "atlas", "role": "general"},
            {"id": "retiring", "role": "engineer"},
        ]
    )
    admin.list_routines = AsyncMock(
        return_value=[
            {"id": "routine-live", "assigneeAgentId": "retiring", "status": "active"},
            {"id": "routine-old", "assigneeAgentId": "retiring", "status": "archived"},
            {"id": "routine-other", "assigneeAgentId": "atlas", "status": "active"},
        ]
    )
    admin.patch_routine = AsyncMock(return_value={})
    admin.list_issues = AsyncMock(
        side_effect=[
            [
                {"id": "issue-open", "status": "in_review"},
                {"id": "issue-done", "status": "done"},
            ],
            [
                {"id": "issue-cancelled", "status": "cancelled"},
            ],
        ]
    )
    admin.patch_issue = AsyncMock(return_value={})
    http = MagicMock()
    http.aclose = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_company_id_for_owner", new=AsyncMock(return_value="company")),
        patch.object(lifecycle, "PaperclipAdminClient", return_value=admin),
        patch.object(lifecycle, "get_admin_session_cookie", new=AsyncMock(return_value="cookie")),
        patch.object(lifecycle.httpx, "AsyncClient", return_value=http),
        patch.object(lifecycle, "_ISSUE_PAGE_SIZE", 2),
        patch(
            "core.services.paperclip_openclaw_pairing.get_seeded_agent_ids",
            new=AsyncMock(return_value={"friday", "atlas"}),
        ),
    ):
        result = await lifecycle.prepare_paperclip_agent_delete(
            owner_id="owner",
            agent_id="retiring",
        )

    assert result == {
        "replacement_agent_id": "friday",
        "issues_reassigned": 1,
        "routines_archived": 2,
    }
    assert admin.patch_routine.await_args_list[0].kwargs["body"] == {
        "assigneeAgentId": "friday",
        "status": "archived",
    }
    assert admin.patch_routine.await_args_list[1].kwargs["body"] == {
        "assigneeAgentId": "friday",
    }
    admin.patch_issue.assert_awaited_once_with(
        issue_id="issue-open",
        body={"assigneeAgentId": "friday"},
        session_cookie="cookie",
    )
    assert [call.kwargs["params"]["offset"] for call in admin.list_issues.await_args_list] == ["0", "2"]


@pytest.mark.asyncio
async def test_delete_everywhere_removes_runtime_config_workspace_then_paperclip():
    events: list[str] = []
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "token"}, "10.0.0.7"))
    gateway = MagicMock()
    gateway.send_rpc = AsyncMock(side_effect=lambda **_: events.append("gateway"))
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock(side_effect=lambda *_: events.append("sidecar"))

    async def record(name: str):
        events.append(name)

    async def record_config(*_args, **_kwargs):
        await record("config")

    async def record_workspace(*_args, **_kwargs):
        await record("workspace")

    async def record_paperclip(**_kwargs):
        await record("paperclip")

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.is_seeded_agent",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            lifecycle,
            "prepare_paperclip_agent_delete",
            new=AsyncMock(return_value={"issues_reassigned": 1, "routines_archived": 1}),
        ),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_gateway_pool", return_value=gateway),
        patch("core.containers.get_workspace", return_value=workspace),
        patch(
            "core.services.config_patcher.remove_from_openclaw_config_list",
            new=AsyncMock(side_effect=record_config),
        ),
        patch(
            "core.containers.workspace.cleanup_deleted_agent",
            new=AsyncMock(side_effect=record_workspace),
        ),
        patch.object(
            lifecycle,
            "delete_paperclip_for_chat",
            new=AsyncMock(side_effect=record_paperclip),
        ),
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert events == ["gateway", "config", "sidecar", "workspace", "paperclip"]
    assert result["deleted"] is True
    rpc = gateway.send_rpc.await_args.kwargs
    assert rpc["method"] == "agents.delete"
    assert rpc["params"] == {"agentId": "retiring", "deleteFiles": True}


@pytest.mark.asyncio
async def test_delete_everywhere_reconciles_persistence_when_container_is_stopped():
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    gateway = MagicMock()
    gateway.send_rpc = AsyncMock()
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock()

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.is_seeded_agent",
            new=AsyncMock(return_value=False),
        ),
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_gateway_pool", return_value=gateway),
        patch("core.containers.get_workspace", return_value=workspace),
        patch(
            "core.services.config_patcher.remove_from_openclaw_config_list",
            new=AsyncMock(),
        ) as remove_config,
        patch(
            "core.containers.workspace.cleanup_deleted_agent",
            new=AsyncMock(),
        ),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result["deleted"] is True
    gateway.send_rpc.assert_not_awaited()
    remove_config.assert_awaited_once()
    workspace.remove_template_sidecar.assert_called_once_with("owner", "retiring")
    delete_paperclip.assert_awaited_once_with(owner_id="owner", agent_id="retiring")


@pytest.mark.asyncio
async def test_delete_everywhere_keeps_paperclip_handle_when_sidecar_removal_fails():
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock(side_effect=OSError("efs unavailable"))

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.is_seeded_agent",
            new=AsyncMock(return_value=False),
        ),
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=workspace),
        patch(
            "core.services.config_patcher.remove_from_openclaw_config_list",
            new=AsyncMock(),
        ),
        patch(
            "core.containers.workspace.cleanup_deleted_agent",
            new=AsyncMock(),
        ) as cleanup,
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        with pytest.raises(OSError, match="efs unavailable"):
            await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    cleanup.assert_not_awaited()
    delete_paperclip.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_everywhere_fails_closed_for_seed_agent():
    with patch(
        "core.services.paperclip_openclaw_pairing.is_seeded_agent",
        new=AsyncMock(return_value=True),
    ):
        with pytest.raises(PermissionError, match="Seeded agents"):
            await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="friday")
