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

    async def _gw_call(_owner_id, method, _params, **_kwargs):
        if method == "agents.list":
            return {"agents": [{"id": "retiring"}]}
        events.append("gateway")
        return {}

    gw_call = AsyncMock(side_effect=_gw_call)
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
        patch.object(lifecycle.container_rpc, "call", gw_call),
        patch("core.containers.get_workspace", return_value=workspace),
        patch(
            "core.services.config_patcher.remove_from_openclaw_config_list",
            new=AsyncMock(side_effect=record_config),
        ),
        patch(
            "core.services.config_patcher.deregister_lightbulb_agent_account",
            new=AsyncMock(),
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
    assert [call.args[1] for call in gw_call.await_args_list] == ["agents.list", "agents.delete"]
    delete_call = gw_call.await_args_list[1]
    assert delete_call.args[1] == "agents.delete"
    assert delete_call.args[2] == {"agentId": "retiring", "deleteFiles": True}


@pytest.mark.asyncio
async def test_delete_everywhere_retries_after_runtime_agent_is_already_absent():
    events: list[str] = []
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "token"}, "10.0.0.7"))
    gw_call = AsyncMock(return_value={"agents": [{"id": "friday"}]})
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock(side_effect=lambda *_: events.append("sidecar"))

    async def record_config(*_args, **_kwargs):
        events.append("config")

    async def record_workspace(*_args, **_kwargs):
        events.append("workspace")

    async def record_paperclip(**_kwargs):
        events.append("paperclip")

    with (
        patch("core.services.paperclip_openclaw_pairing.is_seeded_agent", new=AsyncMock(return_value=False)),
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch.object(lifecycle.container_rpc, "call", gw_call),
        patch("core.containers.get_workspace", return_value=workspace),
        patch(
            "core.services.config_patcher.remove_from_openclaw_config_list",
            new=AsyncMock(side_effect=record_config),
        ),
        patch(
            "core.services.config_patcher.deregister_lightbulb_agent_account",
            new=AsyncMock(),
        ),
        patch(
            "core.containers.workspace.cleanup_deleted_agent",
            new=AsyncMock(side_effect=record_workspace),
        ),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock(side_effect=record_paperclip)),
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result["deleted"] is True
    assert events == ["config", "sidecar", "workspace", "paperclip"]
    gw_call.assert_awaited_once()
    assert gw_call.await_args.args[1] == "agents.list"


@pytest.mark.asyncio
async def test_delete_everywhere_retries_transient_openclaw_file_lock():
    events: list[str] = []
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "token"}, "10.0.0.7"))
    gw_call = AsyncMock(
        side_effect=[
            {"agents": [{"id": "retiring"}]},
            RuntimeError(
                '{"code":"UNAVAILABLE","message":"file lock timeout for '
                '/home/node/.openclaw/openclaw.json: code=file_lock_timeout"}'
            ),
            {},
        ]
    )
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock(side_effect=lambda *_: events.append("sidecar"))

    with (
        patch("core.services.paperclip_openclaw_pairing.is_seeded_agent", new=AsyncMock(return_value=False)),
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch.object(lifecycle.container_rpc, "call", gw_call),
        patch("core.containers.get_workspace", return_value=workspace),
        patch.object(lifecycle.asyncio, "sleep", new=AsyncMock()) as retry_sleep,
        patch(
            "core.services.config_patcher.remove_from_openclaw_config_list",
            new=AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("config")),
        ),
        patch(
            "core.services.config_patcher.deregister_lightbulb_agent_account",
            new=AsyncMock(),
        ),
        patch(
            "core.containers.workspace.cleanup_deleted_agent",
            new=AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("workspace")),
        ),
        patch.object(
            lifecycle,
            "delete_paperclip_for_chat",
            new=AsyncMock(side_effect=lambda **_kwargs: events.append("paperclip")),
        ),
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result["deleted"] is True
    assert events == ["config", "sidecar", "workspace", "paperclip"]
    assert [call.args[1] for call in gw_call.await_args_list] == [
        "agents.list",
        "agents.delete",
        "agents.delete",
    ]
    retry_sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_delete_everywhere_exhausts_file_lock_retries_before_cleanup():
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "token"}, "10.0.0.7"))
    lock_error = RuntimeError("code=file_lock_timeout")
    gw_call = AsyncMock(
        side_effect=[
            {"agents": [{"id": "retiring"}]},
            lock_error,
            lock_error,
            lock_error,
        ]
    )

    with (
        patch("core.services.paperclip_openclaw_pairing.is_seeded_agent", new=AsyncMock(return_value=False)),
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch.object(lifecycle.container_rpc, "call", gw_call),
        patch.object(lifecycle.asyncio, "sleep", new=AsyncMock()) as retry_sleep,
        patch(
            "core.services.config_patcher.remove_from_openclaw_config_list",
            new=AsyncMock(),
        ) as remove_config,
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        with pytest.raises(RuntimeError, match="file_lock_timeout"):
            await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert retry_sleep.await_args_list[0].args == (0.25,)
    assert retry_sleep.await_args_list[1].args == (1.0,)
    assert gw_call.await_count == 4
    remove_config.assert_not_awaited()
    delete_paperclip.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_runtime_agent_does_not_retry_unrelated_gateway_error():
    gw_call = AsyncMock(side_effect=RuntimeError("gateway unavailable"))

    with (
        patch.object(lifecycle.container_rpc, "call", gw_call),
        patch.object(lifecycle.asyncio, "sleep", new=AsyncMock()) as retry_sleep,
    ):
        with pytest.raises(RuntimeError, match="gateway unavailable"):
            await lifecycle.delete_runtime_agent_with_lock_retry(owner_id="owner", agent_id="retiring")

    gw_call.assert_awaited_once()
    retry_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_everywhere_reconciles_persistence_when_container_is_stopped():
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock()

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
        ) as remove_config,
        patch(
            "core.services.config_patcher.deregister_lightbulb_agent_account",
            new=AsyncMock(),
        ),
        patch(
            "core.containers.workspace.cleanup_deleted_agent",
            new=AsyncMock(),
        ),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result["deleted"] is True
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
            "core.services.config_patcher.deregister_lightbulb_agent_account",
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
async def test_delete_everywhere_allows_starter_specialist():
    with (
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager") as get_ecs,
        patch("core.containers.get_workspace") as get_workspace,
        patch("core.services.config_patcher.remove_from_openclaw_config_list", new=AsyncMock()),
        patch("core.services.config_patcher.deregister_lightbulb_agent_account", new=AsyncMock()),
        patch("core.containers.workspace.cleanup_deleted_agent", new=AsyncMock()),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        get_ecs.return_value.resolve_running_container = AsyncMock(return_value=(None, None))
        get_workspace.return_value.remove_template_sidecar = MagicMock()
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="starter-specialist")

    assert result["deleted"] is True
    delete_paperclip.assert_awaited_once_with(owner_id="owner", agent_id="starter-specialist")


# ---------------------------------------------------------------------------
# Q9a row #3 (task-9-scan.md): an AgentCore row has no openclaw.json -- the S3
# profile prefix is the durable record to remove instead.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_everywhere_on_agentcore_row_deletes_s3_profile_not_efs():
    """F4 (predeploy review, M6-R37): the agents.list probe (and the gateway delete it might
    trigger) is an ECS-only concept -- an AgentCore row has no gateway RPC to check first, and
    probing one anyway paid runtime_rpc's 30s default against a possibly-parked microVM for an
    answer this branch never used. The S3 profile delete below is this transport's whole
    story, so container_rpc.call must never be reached at all here."""
    events: list[str] = []
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(
        return_value=({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}, "agentcore")
    )
    gw_call = AsyncMock(side_effect=AssertionError("agents.list must never be probed on an agentcore row"))
    delete_profile = MagicMock(side_effect=lambda *_: events.append("s3"))
    remove_config = AsyncMock(side_effect=lambda *_a, **_k: events.append("config"))
    deregister = AsyncMock(side_effect=lambda *_a, **_k: events.append("deregister"))
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock(side_effect=lambda *_: events.append("sidecar"))

    async def record_workspace(*_a, **_k):
        events.append("workspace")

    async def record_paperclip(**_kwargs):
        events.append("paperclip")

    with (
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=workspace),
        patch.object(lifecycle.container_rpc, "call", gw_call),
        patch("core.services.hermes_profiles.delete_profile", delete_profile),
        patch("core.services.config_patcher.remove_from_openclaw_config_list", remove_config),
        patch("core.services.config_patcher.deregister_lightbulb_agent_account", deregister),
        patch("core.containers.workspace.cleanup_deleted_agent", new=AsyncMock(side_effect=record_workspace)),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock(side_effect=record_paperclip)),
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result["deleted"] is True
    # No EFS call at all -- the openclaw.json-shaped cleanup is entirely skipped.
    assert events == ["s3", "sidecar", "workspace", "paperclip"]
    delete_profile.assert_called_once_with("owner", "retiring")
    remove_config.assert_not_awaited()
    deregister.assert_not_awaited()
    gw_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_everywhere_on_ecs_row_still_removes_from_openclaw_config():
    """The ECS branch stays byte-identical: unchanged EFS cleanup, no S3 profile delete."""
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    workspace = MagicMock()
    workspace.remove_template_sidecar = MagicMock()
    delete_profile = MagicMock()

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.is_seeded_agent",
            new=AsyncMock(return_value=False),
        ),
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=workspace),
        patch("core.services.hermes_profiles.delete_profile", delete_profile),
        patch("core.services.config_patcher.remove_from_openclaw_config_list", new=AsyncMock()) as remove_config,
        patch("core.services.config_patcher.deregister_lightbulb_agent_account", new=AsyncMock()) as deregister,
        patch("core.containers.workspace.cleanup_deleted_agent", new=AsyncMock()),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result["deleted"] is True
    remove_config.assert_awaited_once()
    deregister.assert_awaited_once()
    delete_profile.assert_not_called()
    delete_paperclip.assert_awaited_once_with(owner_id="owner", agent_id="retiring")
